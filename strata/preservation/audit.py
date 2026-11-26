"""在目录恢复或交接时检查声明和引用是否一致。"""

from collections import Counter
from dataclasses import dataclass, field
from sqlite3 import Connection

from strata.runtime.database import Database
from strata.runtime.errors import VaultError
from strata.transfer.deposits import collection_row
from strata.transfer.manifest import collection_id, integer, parse_stored


@dataclass
class Findings:
    limit: int
    checked: Counter = field(default_factory=Counter)
    counts: Counter = field(default_factory=Counter)
    examples: list[dict] = field(default_factory=list)

    def record(self, code: str, **details: object) -> None:
        self.counts[code] += 1
        if len(self.examples) < self.limit:
            self.examples.append({"code": code, **details})

    def as_dict(self) -> dict:
        return {
            "consistent": not self.counts,
            "checked": dict(self.checked),
            "issue_counts": dict(self.counts),
            "issues": self.examples,
            "examples_truncated": sum(self.counts.values()) > self.limit,
        }


class Audit:
    def __init__(self, db: Database):
        self.db = db

    def check(self, collection: str | None, limit: int) -> dict:
        integer(limit, 1, 200, "audit example limit")
        if collection is not None:
            collection_id(collection)
        findings = Findings(limit)
        with self.db.transaction(write=False) as connection:
            if collection is None:
                collections = connection.execute("SELECT * FROM collections ORDER BY name")
            else:
                collections = [collection_row(connection, collection)]
            for row in collections:
                self._check_collection(connection, row, findings)
            if collection is None:
                self._check_object_rows(connection, findings)
                for row in connection.execute("PRAGMA foreign_key_check"):
                    findings.record("foreign_key", table=row[0], rowid=row[1], parent=row[2])
        return {"collection": collection, **findings.as_dict()}

    def _check_collection(self, connection: Connection, collection, findings: Findings) -> None:
        name = collection["name"]
        findings.checked["collections"] += 1
        revisions = connection.execute("SELECT * FROM revisions WHERE collection=? ORDER BY number", (name,))
        previous = 0
        for row in revisions:
            number = row["number"]
            findings.checked["revisions"] += 1
            if number != previous + 1 or row["parent"] != previous:
                findings.record("revision_chain_gap", collection=name, revision=number, previous=previous)
            if row["retain_until"] != row["published_at"] + collection["retention_seconds"]:
                findings.record("retention_differs", collection=name, revision=number)
            if number == collection["head"] and row["retired_at"] is not None:
                findings.record("head_retired", collection=name, revision=number)
            if row["retired_at"] is not None:
                self._check_retired(connection, row, findings)
            self._check_revision_parts(connection, row, findings)
            previous = number
        if collection["head"] != previous:
            findings.record("head_differs", collection=name, head=collection["head"], latest=previous)
        deposits = connection.execute("SELECT * FROM deposits WHERE collection=? ORDER BY id", (name,))
        for deposit in deposits:
            self._check_deposit(connection, deposit, findings)

    @staticmethod
    def _check_retired(connection: Connection, row, findings: Findings) -> None:
        key = (row["collection"], row["number"])
        if row["retired_at"] < row["retain_until"]:
            findings.record("early_retirement", collection=key[0], revision=key[1])
        holds = connection.execute(
            """SELECT reference FROM holds WHERE collection=? AND number=?
               AND (released_at IS NULL OR released_at>?) ORDER BY reference""",
            (*key, row["retired_at"]),
        )
        for hold in holds:
            findings.record("retirement_violates_hold", collection=key[0], revision=key[1],
                            reference=hold["reference"])

    def _check_revision_parts(self, connection: Connection, row, findings: Findings) -> None:
        key = (row["collection"], row["number"])
        parts = connection.execute(
            """SELECT p.digest,o.digest AS stored,o.size,o.sweep_state FROM revision_parts p
               LEFT JOIN objects o ON o.digest=p.digest WHERE p.collection=? AND p.number=? ORDER BY p.digest""",
            key,
        ).fetchall()
        manifest = self._manifest(row, "revision", f"{key[0]}/{key[1]}", findings)
        if manifest is not None:
            self._compare_parts(manifest, {item["digest"]: None for item in parts}, "revision",
                                f"{key[0]}/{key[1]}", findings)
        if row["retired_at"] is None:
            for item in parts:
                wanted = manifest.get(item["digest"]) if manifest is not None else None
                if item["stored"] is None or item["sweep_state"] != "live":
                    findings.record("absent_protected_object", collection=key[0], revision=key[1],
                                    digest=item["digest"])
                elif wanted is not None and item["size"] != wanted:
                    findings.record("protected_object_size", collection=key[0], revision=key[1],
                                    digest=item["digest"], expected=wanted, actual=item["size"])

    def _check_deposit(self, connection: Connection, row, findings: Findings) -> None:
        identifier = row["id"]
        findings.checked["deposits"] += 1
        manifest = self._manifest(row, "deposit", identifier, findings)
        expected = connection.execute("SELECT digest,size FROM expected_parts WHERE deposit=? ORDER BY digest",
                                      (identifier,))
        if manifest is not None:
            self._compare_parts(manifest, {item["digest"]: item["size"] for item in expected}, "deposit",
                                identifier, findings)
        published = connection.execute("SELECT * FROM revisions WHERE deposit=?", (identifier,)).fetchone()
        if row["status"] == "published":
            if published is None or any(published[revision_key] != row[deposit_key]
                                        for revision_key, deposit_key in (
                                            ("number", "revision"), ("collection", "collection"),
                                            ("parent", "parent"), ("manifest_hash", "manifest_hash"),
                                            ("manifest", "manifest"),
                                        )):
                findings.record("published_deposit_mismatch", deposit=identifier)
        elif published is not None:
            findings.record("unexpected_published_revision", deposit=identifier)
        received = connection.execute(
            """SELECT p.digest,p.received_at,e.size AS expected,o.size AS stored,o.sweep_state
               FROM received_parts p JOIN expected_parts e ON e.deposit=p.deposit AND e.digest=p.digest
               JOIN objects o ON o.digest=p.digest WHERE p.deposit=? ORDER BY p.digest""", (identifier,),
        )
        for item in received:
            findings.checked["part_receipts"] += 1
            if row["status"] != "open":
                findings.record("closed_deposit_protects_object", deposit=identifier, digest=item["digest"])
            if (item["stored"] != item["expected"] or item["sweep_state"] != "live"
                    or not row["created_at"] <= item["received_at"] < row["expires_at"]):
                findings.record("part_receipt_mismatch", deposit=identifier, digest=item["digest"])

    @staticmethod
    def _manifest(row, kind: str, identifier: str, findings: Findings) -> dict[str, int] | None:
        try:
            return parse_stored(row["manifest"], row["manifest_hash"]).unique_parts()
        except (VaultError, RuntimeError, ValueError, RecursionError):
            findings.record("invalid_snapshot_manifest", kind=kind, entity=identifier)
            return None

    @staticmethod
    def _compare_parts(expected: dict[str, int], actual: dict[str, int | None], kind: str,
                       identifier: str, findings: Findings) -> None:
        for digest in sorted(expected.keys() - actual.keys()):
            findings.record("missing_manifest_part", kind=kind, entity=identifier, digest=digest)
        for digest in sorted(actual.keys() - expected.keys()):
            findings.record("unexpected_manifest_part", kind=kind, entity=identifier, digest=digest)
        for digest in sorted(actual.keys() & expected.keys()):
            if actual[digest] is not None and actual[digest] != expected[digest]:
                findings.record("manifest_part_size", kind=kind, entity=identifier, digest=digest)

    @staticmethod
    def _check_object_rows(connection: Connection, findings: Findings) -> None:
        for item in connection.execute("SELECT digest,size,added_at,verified_at,marked_at FROM objects ORDER BY digest"):
            findings.checked["objects"] += 1
            if item["verified_at"] < item["added_at"] or (
                item["marked_at"] is not None and item["marked_at"] < item["added_at"]
            ):
                findings.record("object_time_order", digest=item["digest"])
        dangling = connection.execute(
            """SELECT i.id,i.digest FROM incidents i LEFT JOIN objects o ON o.digest=i.digest
               WHERE i.resolved_at IS NULL AND o.digest IS NULL ORDER BY i.id"""
        )
        for item in dangling:
            findings.record("incident_has_no_object", digest=item["digest"], incident=item["id"])
