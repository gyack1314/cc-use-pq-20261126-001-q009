"""管理馆藏头版本以及不可变的出版档案版本。"""

import json
from sqlite3 import Connection

from strata.delivery.assembly import file_blocks
from strata.preservation.integrity import report, verified
from strata.runtime.database import Database
from strata.runtime.errors import VaultError, conflict, invalid, missing
from strata.runtime.events import append
from strata.runtime.media import Media
from strata.transfer.deposits import collection_row, deposit_row, pending_parts, require_open, DAY
from strata.transfer.manifest import collection_id, integer, parse_stored


def revision_row(connection: Connection, collection: str, number: int):
    integer(number, 1, 2**63 - 1, "revision")
    row = connection.execute("SELECT * FROM revisions WHERE collection=? AND number=?",
                             (collection, number)).fetchone()
    if row is None:
        raise missing("revision", f"{collection}/{number}")
    return row


def active_holds(connection: Connection, collection: str, number: int) -> list[dict]:
    return [dict(row) for row in connection.execute(
        """SELECT reference,reason,placed_at FROM holds
           WHERE collection=? AND number=? AND released_at IS NULL ORDER BY reference""",
        (collection, number),
    )]


def damage(connection: Connection, collection: str, number: int) -> list[dict]:
    return [dict(row) for row in connection.execute(
        """SELECT i.id,i.digest,i.reason,i.observed_at FROM revision_parts p JOIN incidents i
           ON i.digest=p.digest AND i.resolved_at IS NULL WHERE p.collection=? AND p.number=?
           ORDER BY i.digest""", (collection, number),
    )]


def describe(connection: Connection, row, *, include_manifest: bool = False) -> dict:
    collection, number = row["collection"], row["number"]
    incidents = damage(connection, collection, number)
    state = "retired" if row["retired_at"] is not None else "degraded" if incidents else "available"
    result = {
        "collection": collection, "revision": number, "parent": row["parent"], "deposit": row["deposit"],
        "published_at": row["published_at"], "retain_until": row["retain_until"], "retired_at": row["retired_at"],
        "manifest_sha256": row["manifest_hash"], "state": state, "incidents": incidents,
        "holds": active_holds(connection, collection, number),
    }
    if include_manifest:
        result["manifest"] = json.loads(row["manifest"])
    return result


class Catalog:
    def __init__(self, db: Database, media: Media):
        self.db = db
        self.media = media

    def create_collection(self, name: str, retention: int) -> dict:
        collection_id(name)
        integer(retention, 60, 3650 * DAY, "retention")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM collections WHERE name=?", (name,)).fetchone()
            if row is not None:
                if row["retention_seconds"] != retention:
                    raise conflict("Collection exists with a different retention period", collection=name)
                return dict(row)
            now = self.db.now()
            connection.execute("INSERT INTO collections(name,retention_seconds,created_at) VALUES(?,?,?)",
                               (name, retention, now))
            append(connection, name, "collection_created", name, now, retention=retention)
            return dict(collection_row(connection, name))

    def collection(self, name: str) -> dict:
        with self.db.transaction(write=False) as connection:
            return dict(collection_row(connection, name))

    def collections(self, after: str, limit: int) -> dict:
        if after:
            collection_id(after)
        if not 1 <= limit <= 200:
            raise invalid("Collection limit must be in 1..200")
        with self.db.transaction(write=False) as connection:
            rows = connection.execute("SELECT * FROM collections WHERE name>? ORDER BY name LIMIT ?",
                                      (after, limit + 1)).fetchall()
            items = [dict(row) for row in rows[:limit]]
            return {"items": items, "next_after": items[-1]["name"] if len(rows) > limit else None}

    def publish(self, deposit: str) -> dict:
        failure = None
        result = None
        with self.db.transaction() as connection:
            row = deposit_row(connection, deposit)
            collection = row["collection"]
            if row["status"] == "published":
                return describe(connection, revision_row(connection, collection, row["revision"]))
            now = self.db.now()
            require_open(row, now)
            current = collection_row(connection, collection)
            if row["parent"] != current["head"]:
                raise conflict("Deposit was based on a superseded revision", current=current["head"],
                               parent=row["parent"])
            pending = pending_parts(connection, deposit)
            if pending:
                raise conflict("Deposit is incomplete", pending=pending)
            manifest = parse_stored(row["manifest"], row["manifest_hash"])
            try:
                for entry in manifest.files:
                    for _ in file_blocks(self.media, entry):
                        pass
            except VaultError as error:
                if error.code == "object_unreadable":
                    report(connection, error, now)
                elif error.code != "file_digest_mismatch":
                    raise
                failure = error
            else:
                number = current["head"] + 1
                connection.execute(
                    """INSERT INTO revisions(collection,number,deposit,parent,manifest_hash,manifest,
                       published_at,retain_until) VALUES(?,?,?,?,?,?,?,?)""",
                    (collection, number, deposit, row["parent"], row["manifest_hash"], row["manifest"],
                     now, now + current["retention_seconds"]),
                )
                parts = manifest.unique_parts()
                connection.executemany(
                    "INSERT INTO revision_parts(collection,number,digest) VALUES(?,?,?)",
                    ((collection, number, digest) for digest in parts),
                )
                for digest in parts:
                    verified(connection, digest, now)
                connection.execute("UPDATE collections SET head=? WHERE name=?", (number, collection))
                connection.execute("UPDATE deposits SET status='published',revision=? WHERE id=?",
                                   (number, deposit))
                connection.execute("DELETE FROM received_parts WHERE deposit=?", (deposit,))
                append(connection, collection, "revision_published", str(number), now,
                       deposit=deposit, parent=row["parent"], manifest_sha256=row["manifest_hash"])
                result = describe(connection, revision_row(connection, collection, number))
        if failure is not None:
            raise failure
        return result

    def revision(self, collection: str, number: int) -> dict:
        with self.db.transaction(write=False) as connection:
            return describe(connection, revision_row(connection, collection, number), include_manifest=True)

    def revisions(self, collection: str, after: int, limit: int) -> dict:
        integer(after, 0, 2**63 - 1, "revision cursor")
        integer(limit, 1, 200, "revision page limit")
        with self.db.transaction(write=False) as connection:
            head = collection_row(connection, collection)["head"]
            rows = connection.execute(
                "SELECT * FROM revisions WHERE collection=? AND number>? ORDER BY number LIMIT ?",
                (collection, after, limit + 1),
            ).fetchall()
            items = [describe(connection, row) for row in rows[:limit]]
            return {"head": head, "items": items,
                    "next_after": items[-1]["revision"] if len(rows) > limit else None}
