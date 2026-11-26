"""以源端令牌和规范化清单标识可续传的档案接收会话。"""

import json
from sqlite3 import Connection
from typing import BinaryIO
from uuid import uuid4

from strata.preservation.integrity import active, report, verified
from strata.runtime.database import Database
from strata.runtime.errors import VaultError, conflict, invalid, missing
from strata.runtime.events import append
from strata.runtime.media import Media
from strata.transfer.manifest import collection_id, integer, parse_manifest, reference_id, sha256

DAY = 86400


def deposit_row(connection: Connection, deposit: str):
    row = connection.execute("SELECT * FROM deposits WHERE id=?", (deposit,)).fetchone()
    if row is None:
        raise missing("deposit", deposit)
    return row


def collection_row(connection: Connection, collection: str):
    row = connection.execute("SELECT * FROM collections WHERE name=?", (collection,)).fetchone()
    if row is None:
        raise missing("collection", collection)
    return row


def effective_state(row, now: int) -> str:
    if row["status"] == "open" and row["expires_at"] <= now:
        return "expired"
    return row["status"]


def require_open(row, now: int) -> None:
    state = effective_state(row, now)
    if state != "open":
        raise conflict("Deposit cannot accept changes", deposit=row["id"], status=state)


def pending_parts(connection: Connection, deposit: str) -> list[dict]:
    return [dict(row) for row in connection.execute(
        """SELECT e.digest,e.size,CASE WHEN i.id IS NULL THEN 'awaiting_upload' ELSE 'damaged' END AS reason
           FROM expected_parts e LEFT JOIN received_parts p ON p.deposit=e.deposit AND p.digest=e.digest
           LEFT JOIN incidents i ON i.digest=e.digest AND i.resolved_at IS NULL
           WHERE e.deposit=? AND (p.digest IS NULL OR i.id IS NOT NULL) ORDER BY e.digest""",
        (deposit,),
    )]


def summary(connection: Connection, row, now: int, *, include_manifest: bool = False) -> dict:
    state = effective_state(row, now)
    result = {
        "id": row["id"], "collection": row["collection"], "token": row["token"],
        "parent": row["parent"], "status": state, "manifest_sha256": row["manifest_hash"],
        "created_at": row["created_at"], "expires_at": row["expires_at"], "revision": row["revision"],
        "pending": pending_parts(connection, row["id"]) if state == "open" else [],
    }
    if include_manifest:
        result["manifest"] = json.loads(row["manifest"])
    return result


class Deposits:
    def __init__(self, db: Database, media: Media):
        self.db = db
        self.media = media

    def open(self, collection: str, token: str, parent: int, lifetime: int, raw: object) -> dict:
        collection_id(collection)
        reference_id(token, "source token")
        integer(parent, 0, 2**63 - 1, "parent revision")
        integer(lifetime, 60, 30 * DAY, "deposit lifetime")
        manifest = parse_manifest(raw)
        fingerprint = manifest.fingerprint()
        with self.db.transaction() as connection:
            now = self.db.now()
            current = collection_row(connection, collection)
            previous = connection.execute(
                "SELECT * FROM deposits WHERE collection=? AND token=?", (collection, token),
            ).fetchone()
            if previous is not None:
                if (previous["manifest_hash"] != fingerprint or previous["parent"] != parent
                        or previous["expires_at"] - previous["created_at"] != lifetime):
                    raise conflict("Source token was used for another deposit", token=token)
                return summary(connection, previous, now)
            if current["head"] != parent:
                raise conflict("Deposit parent differs from current head", current=current["head"], parent=parent)
            deposit = uuid4().hex
            connection.execute(
                """INSERT INTO deposits(id,collection,token,parent,manifest,manifest_hash,created_at,expires_at,status)
                   VALUES(?,?,?,?,?,?,?,?,'open')""",
                (deposit, collection, token, parent, manifest.text(), fingerprint, now, now + lifetime),
            )
            parts = manifest.unique_parts()
            connection.executemany("INSERT INTO expected_parts(deposit,digest,size) VALUES(?,?,?)",
                                   ((deposit, digest, size) for digest, size in parts.items()))
            reused = self._claim_existing(connection, deposit, parts, now)
            append(connection, collection, "deposit_opened", deposit, now,
                   part_count=len(parts), reused_count=reused, parent=parent)
            return summary(connection, deposit_row(connection, deposit), now)

    def _claim_existing(self, connection: Connection, deposit: str, parts: dict[str, int], now: int) -> int:
        count = 0
        for digest, size in parts.items():
            item = connection.execute(
                "SELECT size FROM objects WHERE digest=? AND sweep_state='live'", (digest,),
            ).fetchone()
            if item is None or active(connection, digest) is not None:
                continue
            if item["size"] != size:
                raise invalid("Part length differs from known object length", digest=digest)
            try:
                self.media.verify(digest, size)
            except VaultError as error:
                report(connection, error, now)
                continue
            verified(connection, digest, now)
            connection.execute(
                "INSERT INTO received_parts(deposit,digest,received_at) VALUES(?,?,?)", (deposit, digest, now)
            )
            count += 1
        return count

    def get(self, deposit: str) -> dict:
        with self.db.transaction(write=False) as connection:
            return summary(connection, deposit_row(connection, deposit), self.db.now(), include_manifest=True)

    def required_size(self, deposit: str, digest: str) -> int:
        sha256(digest)
        with self.db.transaction(write=False) as connection:
            require_open(deposit_row(connection, deposit), self.db.now())
            return self._expected_size(connection, deposit, digest)

    def _expected_size(self, connection: Connection, deposit: str, digest: str) -> int:
        row = connection.execute("SELECT size FROM expected_parts WHERE deposit=? AND digest=?",
                                 (deposit, digest)).fetchone()
        if row is None:
            raise missing("expected part", digest)
        return row["size"]

    def receive(self, deposit: str, digest: str, source: BinaryIO) -> dict:
        size = self.required_size(deposit, digest)
        self.media.validate_upload(digest, size, source)
        with self.db.transaction() as connection:
            now = self.db.now()
            row = deposit_row(connection, deposit)
            require_open(row, now)
            obj = connection.execute("SELECT size,sweep_state FROM objects WHERE digest=?", (digest,)).fetchone()
            installed = False
            if obj is not None and obj["size"] != size:
                raise conflict("Known object length differs from declared length", digest=digest)
            if obj is not None and obj["sweep_state"] == "marked":
                raise conflict("Part is being reclaimed, finish sweep before retrying", digest=digest)
            if obj is not None:
                try:
                    self.media.verify(digest, size)
                except VaultError as error:
                    report(connection, error, now)
                    self.media.install(digest, size, source)
                    installed = True
            else:
                self.media.install(digest, size, source)
                connection.execute("INSERT INTO objects(digest,size,added_at,verified_at) VALUES(?,?,?,?)",
                                   (digest, size, now, now))
                installed = True
            verified(connection, digest, now)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO received_parts(deposit,digest,received_at) VALUES(?,?,?)",
                (deposit, digest, now),
            )
            if cursor.rowcount:
                append(connection, row["collection"], "part_received", deposit, now, digest=digest,
                       size=size, stored=installed)
            return {"deposit": deposit, "digest": digest, "size": size,
                    "duplicate": cursor.rowcount == 0, "stored": installed}

    def cancel(self, deposit: str) -> dict:
        with self.db.transaction() as connection:
            now = self.db.now()
            row = deposit_row(connection, deposit)
            if row["status"] == "canceled":
                return summary(connection, row, now)
            require_open(row, now)
            connection.execute("UPDATE deposits SET status='canceled' WHERE id=?", (deposit,))
            connection.execute("DELETE FROM received_parts WHERE deposit=?", (deposit,))
            append(connection, row["collection"], "deposit_canceled", deposit, now)
            return summary(connection, deposit_row(connection, deposit), now)

    def expire(self, limit: int) -> list[str]:
        integer(limit, 1, 500, "expiration limit")
        with self.db.transaction() as connection:
            now = self.db.now()
            rows = connection.execute(
                "SELECT id,collection FROM deposits WHERE status='open' AND expires_at<=? "
                "ORDER BY expires_at,id LIMIT ?", (now, limit),
            ).fetchall()
            for row in rows:
                connection.execute("UPDATE deposits SET status='expired' WHERE id=?", (row["id"],))
                connection.execute("DELETE FROM received_parts WHERE deposit=?", (row["id"],))
                append(connection, row["collection"], "deposit_expired", row["id"], now)
            return [row["id"] for row in rows]
