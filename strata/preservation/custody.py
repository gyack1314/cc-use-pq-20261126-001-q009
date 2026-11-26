"""管理档案保留和可在异常中断后恢复的回收。"""

from pathlib import Path
from sqlite3 import Connection

from strata.preservation.catalog import active_holds, revision_row
from strata.preservation.integrity import resolve
from strata.runtime.database import Database
from strata.runtime.errors import conflict, invalid, missing
from strata.runtime.events import append
from strata.runtime.media import Media, sync_directory
from strata.transfer.deposits import collection_row
from strata.transfer.manifest import integer, reference_id, sha256

UNREFERENCED = """
    NOT EXISTS (
        SELECT 1 FROM revision_parts p JOIN revisions r
        ON p.collection=r.collection AND p.number=r.number
        WHERE p.digest=o.digest AND r.retired_at IS NULL
    )
    AND NOT EXISTS (SELECT 1 FROM received_parts p WHERE p.digest=o.digest)
"""


class Custody:
    def __init__(self, db: Database, media: Media):
        self.db = db
        self.media = media

    def hold(self, collection: str, number: int, reference: str, reason: str) -> dict:
        reference_id(reference)
        if not reason.strip() or len(reason) > 240:
            raise invalid("Hold reason must contain 1..240 characters")
        with self.db.transaction() as connection:
            now = self.db.now()
            revision = revision_row(connection, collection, number)
            if revision["retired_at"] is not None:
                raise conflict("A retired revision cannot receive a hold")
            row = self._hold_row(connection, collection, number, reference)
            if row is not None:
                if row["released_at"] is not None or row["reason"] != reason:
                    raise conflict("Hold reference is already assigned", reference=reference)
                return dict(row)
            connection.execute("INSERT INTO holds(collection,number,reference,reason,placed_at) VALUES(?,?,?,?,?)",
                               (collection, number, reference, reason, now))
            append(connection, collection, "hold_placed", str(number), now, reference=reference)
            return dict(self._hold_row(connection, collection, number, reference))

    def _hold_row(self, connection: Connection, collection: str, number: int, reference: str):
        return connection.execute("SELECT * FROM holds WHERE collection=? AND number=? AND reference=?",
                                  (collection, number, reference)).fetchone()

    def release(self, collection: str, number: int, reference: str) -> dict:
        reference_id(reference)
        with self.db.transaction() as connection:
            revision_row(connection, collection, number)
            row = self._hold_row(connection, collection, number, reference)
            if row is None:
                raise missing("hold", reference)
            if row["released_at"] is not None:
                return dict(row)
            now = self.db.now()
            connection.execute(
                "UPDATE holds SET released_at=? WHERE collection=? AND number=? AND reference=?",
                (now, collection, number, reference),
            )
            append(connection, collection, "hold_released", str(number), now, reference=reference)
            return dict(self._hold_row(connection, collection, number, reference))

    def retire(self, collection: str, number: int) -> dict:
        with self.db.transaction() as connection:
            now = self.db.now()
            revision = revision_row(connection, collection, number)
            head = collection_row(connection, collection)["head"]
            if revision["retired_at"] is not None:
                return dict(revision)
            blockers = []
            if number == head:
                blockers.append("current_head")
            if now < revision["retain_until"]:
                blockers.append("retention")
            holds = active_holds(connection, collection, number)
            if holds:
                blockers.append("active_holds")
            if blockers:
                raise conflict("Revision is still protected", blockers=blockers,
                               retain_until=revision["retain_until"], holds=holds)
            connection.execute("UPDATE revisions SET retired_at=? WHERE collection=? AND number=?",
                               (now, collection, number))
            append(connection, collection, "revision_retired", str(number), now,
                   manifest_sha256=revision["manifest_hash"])
            return dict(revision_row(connection, collection, number))

    def forecast(self, limit: int) -> dict:
        integer(limit, 1, 200, "forecast limit")
        with self.db.transaction(write=False) as connection:
            totals = connection.execute(
                """SELECT COUNT(*) AS object_count,COALESCE(SUM(size),0) AS object_bytes,
                   COALESCE(SUM(sweep_state='marked'),0) AS marked_count FROM objects"""
            ).fetchone()
            query = "SELECT * FROM objects o WHERE " + UNREFERENCED
            reclaimable = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(size),0) AS bytes FROM (" + query + ")",
            ).fetchone()
            items = connection.execute(query + " ORDER BY added_at,digest LIMIT ?", (limit,)).fetchall()
            return {**dict(totals), "unreferenced": dict(reclaimable), "examples": [dict(row) for row in items]}

    def mark(self, limit: int) -> list[str]:
        integer(limit, 1, 200, "mark limit")
        with self.db.transaction() as connection:
            now = self.db.now()
            rows = connection.execute(
                "SELECT o.digest FROM objects o WHERE sweep_state='live' AND " +
                UNREFERENCED + " ORDER BY added_at,digest LIMIT ?", (limit,),
            ).fetchall()
            for row in rows:
                digest = row["digest"]
                connection.execute("UPDATE objects SET sweep_state='marked',marked_at=? WHERE digest=?",
                                   (now, digest))
                append(connection, None, "object_marked", digest, now)
            return [row["digest"] for row in rows]

    def sweep(self, limit: int) -> dict:
        integer(limit, 1, 200, "sweep limit")
        removed = []
        with self.db.transaction(write=False) as connection:
            candidates = connection.execute(
                "SELECT digest FROM objects WHERE sweep_state='marked' ORDER BY marked_at,digest LIMIT ?", (limit,),
            ).fetchall()
        for candidate in candidates:
            digest = candidate["digest"]
            with self.db.transaction() as connection:
                row = connection.execute(
                    "SELECT size FROM objects o WHERE digest=? AND sweep_state='marked' AND " + UNREFERENCED,
                    (digest,),
                ).fetchone()
                if row is None:
                    continue
                self.media.remove(digest)
                connection.execute("DELETE FROM objects WHERE digest=?", (digest,))
                now = self.db.now()
                resolve(connection, digest, now, "reclaimed")
                append(connection, None, "object_reclaimed", digest, now, size=row["size"])
                removed.append({"digest": digest, "size": row["size"]})
        return {"removed": removed, "bytes": sum(item["size"] for item in removed)}

    def reconcile(self, after: str, limit: int, *, remove: bool) -> dict:
        """核对在目录事务中断后可能残留的物理对象。"""
        if after:
            sha256(after)
        integer(limit, 1, 200, "reconciliation limit")
        with self.db.transaction(write=remove) as connection:
            inventory = []
            for digest in self.media.inventory(after):
                inventory.append(digest)
                if len(inventory) > limit:
                    break
            orphans = []
            for digest in inventory[:limit]:
                row = connection.execute("SELECT 1 FROM objects WHERE digest=?", (digest,)).fetchone()
                if row is None:
                    orphans.append(digest)
                    if remove:
                        self.media.remove(digest)
                        append(connection, None, "orphan_removed", digest, self.db.now())
            return {"examined": min(limit, len(inventory)), "orphans": orphans, "removed": remove,
                    "next_after": inventory[limit - 1] if len(inventory) > limit else None}

    def clean_staging(self, limit: int) -> list[str]:
        integer(limit, 1, 200, "staging cleanup limit")
        names = []
        with self.db.transaction() as connection:
            for path in self.media.root.glob("[a-f0-9][a-f0-9]/.pending-*"):
                if not path.is_file() or not self._staging_name(path):
                    continue
                path.unlink()
                sync_directory(path.parent)
                names.append(str(path.relative_to(self.media.root).as_posix()))
                if len(names) >= limit:
                    break
            if names:
                append(connection, None, "staging_cleaned", "media", self.db.now(), names=names)
        return names

    @staticmethod
    def _staging_name(path: Path) -> bool:
        suffix = path.name.removeprefix(".pending-")
        return len(suffix) == 32 and all(character in "0123456789abcdef" for character in suffix)
