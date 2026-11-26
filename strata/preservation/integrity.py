"""记录介质损坏，并在校验修复字节后恢复可取回状态。"""

from sqlite3 import Connection
from typing import BinaryIO

from strata.runtime.database import Database
from strata.runtime.errors import VaultError, conflict, invalid, missing
from strata.runtime.events import append
from strata.runtime.media import Media
from strata.transfer.manifest import sha256


def active(connection: Connection, digest: str):
    return connection.execute(
        "SELECT * FROM incidents WHERE digest=? AND resolved_at IS NULL", (digest,)
    ).fetchone()


def report(connection: Connection, error: VaultError, now: int) -> None:
    if error.code != "object_unreadable":
        raise error
    digest = error.details["digest"]
    reason = error.details["reason"]
    incident = active(connection, digest)
    if incident is None:
        cursor = connection.execute(
            "INSERT INTO incidents(digest,opened_at,observed_at,reason) VALUES(?,?,?,?)",
            (digest, now, now, reason),
        )
        append(connection, None, "object_damaged", digest, now, incident=cursor.lastrowid, reason=reason)
    else:
        connection.execute("UPDATE incidents SET observed_at=?,reason=? WHERE id=?",
                           (now, reason, incident["id"]))


def resolve(connection: Connection, digest: str, now: int, resolution: str) -> None:
    incident = active(connection, digest)
    if incident is not None:
        connection.execute("UPDATE incidents SET resolved_at=?,resolution=? WHERE id=?",
                           (now, resolution, incident["id"]))
        append(connection, None, "object_incident_closed", digest, now,
               incident=incident["id"], resolution=resolution)


def verified(connection: Connection, digest: str, now: int) -> None:
    connection.execute("UPDATE objects SET verified_at=? WHERE digest=?", (now, digest))
    resolve(connection, digest, now, "verified")


class Integrity:
    def __init__(self, db: Database, media: Media):
        self.db = db
        self.media = media

    def scan(self, after: str = "", limit: int = 25) -> dict:
        if after:
            sha256(after)
        if not 1 <= limit <= 50:
            raise invalid("Scan limit must be in 1..50")
        with self.db.transaction() as connection:
            now = self.db.now()
            rows = connection.execute(
                "SELECT digest,size FROM objects WHERE sweep_state='live' AND digest>? "
                "ORDER BY digest LIMIT ?", (after, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                digest = row["digest"]
                try:
                    self.media.verify(digest, row["size"])
                except VaultError as error:
                    report(connection, error, now)
                    items.append({"digest": digest, "condition": error.details["reason"]})
                else:
                    verified(connection, digest, now)
                    items.append({"digest": digest, "condition": "verified"})
            return {"items": items, "next_after": items[-1]["digest"] if len(rows) > limit else None}

    def repair_size(self, digest: str, incident_id: int) -> int:
        sha256(digest)
        with self.db.transaction(write=False) as connection:
            row = self._repair_target(connection, digest, incident_id)
            return row["size"]

    def _repair_target(self, connection: Connection, digest: str, incident_id: int):
        row = connection.execute("SELECT * FROM objects WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise missing("archive object", digest)
        incident = active(connection, digest)
        if incident is None or incident["id"] != incident_id or row["sweep_state"] != "live":
            raise conflict("Repair no longer targets an active incident", digest=digest,
                           incident=incident_id)
        return row

    def repair(self, digest: str, incident_id: int, source: BinaryIO) -> dict:
        size = self.repair_size(digest, incident_id)
        self.media.validate_upload(digest, size, source)
        with self.db.transaction() as connection:
            self._repair_target(connection, digest, incident_id)
            now = self.db.now()
            self.media.install(digest, size, source)
            connection.execute("UPDATE objects SET verified_at=? WHERE digest=?", (now, digest))
            resolve(connection, digest, now, "replaced")
            return {"digest": digest, "incident": incident_id, "verified_at": now}

    def incidents(self, *, after: int, limit: int, open_only: bool) -> dict:
        if not 1 <= limit <= 200 or not 0 <= after <= 2**63 - 1:
            raise invalid("Incident page limits are invalid")
        with self.db.transaction(write=False) as connection:
            query = "SELECT * FROM incidents WHERE id>?"
            if open_only:
                query += " AND resolved_at IS NULL"
            rows = connection.execute(query + " ORDER BY id LIMIT ?", (after, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                impacted = connection.execute(
                    """SELECT r.collection,r.number,r.retired_at FROM revisions r
                       JOIN revision_parts p ON p.collection=r.collection AND p.number=r.number
                       WHERE p.digest=? ORDER BY r.collection,r.number""", (row["digest"],),
                ).fetchall()
                item = dict(row)
                item["revisions"] = [dict(revision) for revision in impacted]
                items.append(item)
            return {"items": items, "next_after": items[-1]["id"] if len(rows) > limit else None}
