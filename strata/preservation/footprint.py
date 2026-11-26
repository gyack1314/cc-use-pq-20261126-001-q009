"""计算档案占用量，避免重复计入共享对象。"""

from sqlite3 import Connection

from strata.runtime.database import Database
from strata.transfer.deposits import collection_row


class Footprint:
    def __init__(self, db: Database):
        self.db = db

    def collection(self, name: str) -> dict:
        with self.db.transaction(write=False) as connection:
            collection = collection_row(connection, name)
            editions = self._editions(connection, name)
            claimed = self._claimed(connection, name)
            local = self._objects(connection, name)
            return {
                "collection": name,
                "head": collection["head"],
                "editions": editions,
                "deposits": claimed,
                "objects": local,
            }

    @staticmethod
    def _editions(connection: Connection, collection: str) -> dict:
        row = connection.execute(
            """SELECT COUNT(*) AS total,COALESCE(SUM(retired_at IS NULL),0) AS live,
               COALESCE(SUM(retired_at IS NOT NULL),0) AS retired FROM revisions WHERE collection=?""",
            (collection,),
        ).fetchone()
        return dict(row)

    @staticmethod
    def _claimed(connection: Connection, collection: str) -> dict:
        row = connection.execute(
            """SELECT COUNT(DISTINCT d.id) AS receipt_holders,COUNT(*) AS receipts,
               COALESCE(SUM(o.size),0) AS claimed_bytes_with_repetition
               FROM deposits d JOIN received_parts p ON p.deposit=d.id
               JOIN objects o ON o.digest=p.digest WHERE d.collection=?""", (collection,),
        ).fetchone()
        return dict(row)

    @staticmethod
    def _objects(connection: Connection, collection: str) -> dict:
        rows = connection.execute(
            """WITH local_edition AS (
                SELECT DISTINCT p.digest FROM revisions r JOIN revision_parts p
                ON p.collection=r.collection AND p.number=r.number
                WHERE r.collection=? AND r.retired_at IS NULL
            ), local_deposit AS (
                SELECT DISTINCT p.digest FROM deposits d JOIN received_parts p ON p.deposit=d.id
                WHERE d.collection=?
            ), combined AS (
                SELECT digest FROM local_edition UNION SELECT digest FROM local_deposit
            )
            SELECT c.digest,o.size,e.digest IS NOT NULL AS edition,p.digest IS NOT NULL AS deposit,
                EXISTS(SELECT 1 FROM revision_parts rp JOIN revisions r
                     ON r.collection=rp.collection AND r.number=rp.number
                     WHERE rp.digest=c.digest AND rp.collection<>? AND r.retired_at IS NULL)
                OR EXISTS(SELECT 1 FROM received_parts cp JOIN deposits d ON d.id=cp.deposit
                     WHERE cp.digest=c.digest AND d.collection<>?) AS external,
                EXISTS(SELECT 1 FROM incidents i WHERE i.digest=c.digest
                     AND i.resolved_at IS NULL) AS damaged
            FROM combined c LEFT JOIN objects o ON o.digest=c.digest
            LEFT JOIN local_edition e ON e.digest=c.digest LEFT JOIN local_deposit p ON p.digest=c.digest""",
            (collection, collection, collection, collection),
        )
        counters = {
            key: {"count": 0, "bytes": 0} for key in
            ("total", "edition", "deposit_only", "shared_outside_collection", "exclusive", "damaged", "missing")
        }

        def tally(key: str, size: int) -> None:
            counters[key]["count"] += 1
            counters[key]["bytes"] += size

        for row in rows:
            size = row["size"]
            if size is None:
                tally("missing", 0)
                continue
            tally("total", size)
            tally("edition" if row["edition"] else "deposit_only", size)
            tally("shared_outside_collection" if row["external"] else "exclusive", size)
            if row["damaged"]:
                tally("damaged", size)
        return counters
