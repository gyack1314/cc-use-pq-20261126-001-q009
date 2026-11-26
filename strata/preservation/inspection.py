"""查询版本覆盖及物理对象受哪些引用保护。"""

from sqlite3 import Connection

from strata.preservation.catalog import revision_row
from strata.runtime.database import Database
from strata.runtime.errors import missing
from strata.transfer.manifest import integer, parse_stored, sha256


class Inspection:
    def __init__(self, db: Database):
        self.db = db

    def coverage(self, collection: str, number: int) -> dict:
        with self.db.transaction(write=False) as connection:
            row = revision_row(connection, collection, number)
            manifest = parse_stored(row["manifest"], row["manifest_hash"])
            conditions = {
                record["digest"]: ("removed" if record["sweep_state"] is None else
                                   "damaged" if record["incident"] is not None else record["sweep_state"])
                for record in connection.execute(
                    """SELECT p.digest,o.sweep_state,i.id AS incident
                       FROM revision_parts p LEFT JOIN objects o ON o.digest=p.digest
                       LEFT JOIN incidents i ON i.digest=p.digest AND i.resolved_at IS NULL
                       WHERE p.collection=? AND p.number=?""", (collection, number),
                )
            }
            files = []
            for entry in manifest.files:
                missing_parts = [part.digest for part in entry.parts
                                 if conditions.get(part.digest) != "live"]
                files.append({
                    "path": entry.path, "sha256": entry.digest, "size": entry.size,
                    "unavailable_parts": sorted(set(missing_parts)),
                    "restorable": row["retired_at"] is None and not missing_parts,
                })
            return {"collection": collection, "revision": number, "manifest_sha256": row["manifest_hash"],
                    "logical_bytes": sum(entry.size for entry in manifest.files),
                    "unique_object_bytes": sum(manifest.unique_parts().values()),
                    "retired": row["retired_at"] is not None, "files": files}

    def object_references(self, digest: str, limit: int) -> dict:
        sha256(digest)
        integer(limit, 1, 200, "reference limit")
        with self.db.transaction(write=False) as connection:
            item = connection.execute("SELECT * FROM objects WHERE digest=?", (digest,)).fetchone()
            editions = self._edition_references(connection, digest, limit)
            deposits = self._deposit_references(connection, digest, limit)
            if item is None and not editions["count"] and not deposits["count"]:
                raise missing("archive object", digest)
            return {
                "digest": digest, "physical": dict(item) if item is not None else None,
                "editions": editions, "deposits": deposits,
            }

    @staticmethod
    def _edition_references(connection: Connection, digest: str, limit: int) -> dict:
        source = """
            FROM revision_parts p JOIN revisions r ON r.collection=p.collection AND r.number=p.number
            JOIN collections c ON c.name=r.collection WHERE p.digest=?
        """
        totals = connection.execute(
            "SELECT COUNT(*) AS count,COALESCE(SUM(r.retired_at IS NULL),0) AS protecting " + source, (digest,),
        ).fetchone()
        examples = connection.execute(
            """SELECT r.collection,r.number,r.retired_at,c.head=r.number AS is_head,
               (SELECT COUNT(*) FROM holds h WHERE h.collection=r.collection AND h.number=r.number
                AND h.released_at IS NULL) AS active_holds """ + source +
            " ORDER BY (r.retired_at IS NOT NULL),r.collection,r.number LIMIT ?", (digest, limit),
        ).fetchall()
        return {**dict(totals), "items": [dict(row) for row in examples]}

    @staticmethod
    def _deposit_references(connection: Connection, digest: str, limit: int) -> dict:
        source = """
            FROM expected_parts e JOIN deposits d ON d.id=e.deposit
            LEFT JOIN received_parts p ON p.deposit=e.deposit AND p.digest=e.digest WHERE e.digest=?
        """
        totals = connection.execute(
            "SELECT COUNT(*) AS count,COALESCE(SUM(p.digest IS NOT NULL),0) AS protecting " + source, (digest,),
        ).fetchone()
        examples = connection.execute(
            """SELECT d.id,d.collection,d.status,d.expires_at,p.received_at """ + source +
            " ORDER BY (p.digest IS NULL),d.id LIMIT ?", (digest, limit),
        ).fetchall()
        return {**dict(totals), "items": [dict(row) for row in examples]}

    def compare(self, collection: str, old_number: int, new_number: int) -> dict:
        with self.db.transaction(write=False) as connection:
            old = revision_row(connection, collection, old_number)
            new = revision_row(connection, collection, new_number)
            old_manifest = parse_stored(old["manifest"], old["manifest_hash"])
            new_manifest = parse_stored(new["manifest"], new["manifest_hash"])
        previous = {entry.path: entry for entry in old_manifest.files}
        current = {entry.path: entry for entry in new_manifest.files}
        comparison = {"added": [], "removed": [], "content_changed": [], "layout_changed": []}
        for name in sorted(set(previous) | set(current)):
            before, after = previous.get(name), current.get(name)
            if before is None:
                comparison["added"].append(after.as_dict())
            elif after is None:
                comparison["removed"].append(before.as_dict())
            elif before.digest != after.digest:
                comparison["content_changed"].append({"before": before.as_dict(), "after": after.as_dict()})
            elif before.parts != after.parts:
                comparison["layout_changed"].append({"before": before.as_dict(), "after": after.as_dict()})
        return {"collection": collection, "from_revision": old_number, "to_revision": new_number,
                "from_manifest_sha256": old["manifest_hash"], "to_manifest_sha256": new["manifest_hash"],
                **comparison}
