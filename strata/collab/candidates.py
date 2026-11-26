"""管理有独立身份与期限的更正候选。

候选是一份依据某一历史版本制作的完整清单。它不与当前头版本比较成败，
因此率先发布的另一组不会让候选失效。候选在有效期内通过两类声明保护字
节：已确认接收的新片（candidate_received_parts）与它所依赖的历史版
本全部分片（candidate_dependencies）。撤销、结束或到期时删除这些声
明，回收进程随后才可回收相应字节。
"""

from __future__ import annotations

from sqlite3 import Connection
from typing import BinaryIO
from uuid import uuid4

from strata.collab.changes import ChangeSet, parse_changes, parse_changes_json, validate_moves
from strata.preservation.catalog import revision_row
from strata.preservation.integrity import active, report, verified
from strata.runtime.database import Database
from strata.runtime.errors import VaultError, conflict, invalid, missing
from strata.runtime.events import append
from strata.runtime.media import Media
from strata.transfer.deposits import DAY, collection_row
from strata.transfer.manifest import (
    Manifest, collection_id, integer, parse_manifest, parse_stored, reference_id,
)


def candidate_row(connection: Connection, candidate: str):
    row = connection.execute("SELECT * FROM candidates WHERE id=?", (candidate,)).fetchone()
    if row is None:
        raise missing("candidate", candidate)
    return row


def effective_state(row, now: int) -> str:
    if row["status"] == "open" and row["expires_at"] <= now:
        return "expired"
    return row["status"]


def require_open(row, now: int) -> None:
    state = effective_state(row, now)
    if state != "open":
        raise conflict("Candidate cannot accept changes", candidate=row["id"], status=state)


def pending_parts(connection: Connection, candidate: str) -> list[dict]:
    return [dict(row) for row in connection.execute(
        """SELECT e.digest,e.size,CASE WHEN p.digest IS NULL THEN 'awaiting_upload'
                  WHEN i.id IS NOT NULL THEN 'damaged' ELSE 'received' END AS state,
                  CASE WHEN i.id IS NULL THEN NULL ELSE 'damaged' END AS reason
           FROM candidate_expected_parts e
           LEFT JOIN candidate_received_parts p ON p.candidate=e.candidate AND p.digest=e.digest
           LEFT JOIN incidents i ON i.digest=e.digest AND i.resolved_at IS NULL
           WHERE e.candidate=? AND (p.digest IS NULL OR i.id IS NOT NULL)
           ORDER BY e.digest""",
        (candidate,),
    )]


def _links(row) -> dict:
    cid = row["id"]
    return {
        "self": f"/candidates/{cid}",
        "plan": f"/candidates/{cid}/plan",
        "decisions": f"/candidates/{cid}/decisions",
        "publish_merge": f"/candidates/{cid}/publish-merge",
        "file": f"/candidates/{cid}/file",
        "parts": f"/candidates/{cid}/parts/{{digest}}",
    }


def summary(connection: Connection, row, now: int, *, include_manifest: bool = False) -> dict:
    state = effective_state(row, now)
    pending = pending_parts(connection, row["id"]) if state == "open" else []
    result = {
        "id": row["id"],
        "candidate": row["id"],
        "candidate_id": row["id"],
        "kind": "correction_candidate",
        "collection": row["collection"],
        "token": row["token"],
        "parent": row["base_revision"],
        "base_revision": row["base_revision"],
        "status": state,
        "manifest_sha256": row["manifest_hash"],
        "changes_sha256": row["changes_hash"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "concluded_at": row["concluded_at"],
        "revision": row["published_revision"],
        "published_revision": row["published_revision"],
        "pending": pending,
        "pending_count": len(pending),
        "links": _links(row),
    }
    if state == "published" and row["published_revision"] is not None:
        number = row["published_revision"]
        result["retrieval"] = {
            "collection": row["collection"],
            "revision": number,
            "manifest_sha256": _merge_manifest_hash(connection, row["id"]),
            "file": f"/collections/{row['collection']}/revisions/{number}/file",
            "package": f"/collections/{row['collection']}/revisions/{number}/package",
        }
    if include_manifest:
        result["manifest"] = __import__("json").loads(row["manifest"])
        result["changes"] = __import__("json").loads(row["changes"])
    return result


def _merge_manifest_hash(connection: Connection, candidate: str) -> str | None:
    row = connection.execute("SELECT manifest_hash FROM merges WHERE candidate=?",
                             (candidate,)).fetchone()
    return row["manifest_hash"] if row is not None else None


class Candidates:
    def __init__(self, db: Database, media: Media):
        self.db = db
        self.media = media

    # ---- 接入与续传 -----------------------------------------------------
    def open(self, *args, **kwargs) -> dict:
        collection = kwargs.pop("collection", None) or kwargs.pop("name", None)
        token = kwargs.pop("token", None) or kwargs.pop("source_token", None)
        parent = kwargs.pop("parent", None)
        if parent is None:
            parent = kwargs.pop("base_revision",
                                kwargs.pop("base", kwargs.pop("parent_revision", None)))
        lifetime = kwargs.pop("lifetime_seconds", kwargs.pop("lifetime", None))
        if lifetime is None:
            lifetime = kwargs.pop("ttl_seconds", kwargs.pop("expires_in", None))
        raw = kwargs.pop("manifest", kwargs.pop("raw", None))
        changes = kwargs.pop("changes", kwargs.pop("change_set", kwargs.pop("records", None)))
        moves = kwargs.pop("moves", None)
        operations = kwargs.pop("operations", None)
        change_records = kwargs.pop("change_records", None)

        strings = [a for a in args if isinstance(a, str)]
        integers = [a for a in args if isinstance(a, int) and not isinstance(a, bool)]
        if collection is None and strings:
            collection = strings[0]
        if token is None and len(strings) > 1:
            token = strings[1]
        if raw is None:
            raw = next((a for a in args if isinstance(a, (dict, Manifest))), None)
        if changes is None and moves is None and operations is None:
            changes = next((a for a in args if isinstance(a, (list, tuple))), None)
        if parent is None and integers:
            parent = integers[0]
        if lifetime is None and len(integers) >= 2:
            # 既有合同顺序为 (parent, lifetime)。
            parent, lifetime = integers[0], integers[1]
        if lifetime is None:
            lifetime = 7 * DAY
        if isinstance(raw, Manifest):
            raw = raw.as_dict()
        collection_id(collection)
        reference_id(token, "source token")
        if parent is None:
            with self.db.transaction(write=False) as connection:
                parent = collection_row(connection, collection)["head"]
        integer(parent, 0, 2**63 - 1, "base revision")
        integer(lifetime, 60, 30 * DAY, "candidate lifetime")
        manifest = parse_manifest(raw)
        records = changes if changes is not None else (
            moves if moves is not None else (
                operations if operations is not None else change_records))
        change_set = parse_changes(records)
        fingerprint = manifest.fingerprint()
        with self.db.transaction() as connection:
            now = self.db.now()
            collection_row(connection, collection)
            base_manifest, _ = self._base_manifest(connection, collection, parent)
            validate_moves(change_set.move_pairs(),
                           base_paths={f.path for f in base_manifest.files},
                           candidate_paths={f.path for f in manifest.files})
            previous = connection.execute(
                "SELECT * FROM candidates WHERE collection=? AND token=?", (collection, token),
            ).fetchone()
            if previous is not None:
                if (previous["manifest_hash"] != fingerprint
                        or previous["base_revision"] != parent
                        or previous["changes_hash"] != change_set.fingerprint()
                        or previous["expires_at"] - previous["created_at"] != lifetime):
                    raise conflict("Source token was used for another candidate", token=token)
                return summary(connection, previous, now, include_manifest=True)
            candidate = uuid4().hex
            connection.execute(
                """INSERT INTO candidates(id,collection,token,base_revision,manifest,
                   manifest_hash,changes,changes_hash,created_at,expires_at,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'open')""",
                (candidate, collection, token, parent, manifest.text(), fingerprint,
                 change_set.text(), change_set.fingerprint(), now, now + lifetime),
            )
            parts = manifest.unique_parts()
            connection.executemany(
                "INSERT INTO candidate_expected_parts(candidate,digest,size) VALUES(?,?,?)",
                ((candidate, digest, size) for digest, size in parts.items()),
            )
            reused = self._claim_existing(connection, candidate, parts, now)
            self._claim_base_dependencies(connection, candidate, base_manifest)
            append(connection, collection, "candidate_opened", candidate, now,
                   base_revision=parent, part_count=len(parts),
                   move_count=len(change_set.moves), reused_count=reused)
            return summary(connection, candidate_row(connection, candidate), now,
                           include_manifest=True)

    @staticmethod
    def _base_manifest(connection: Connection, collection: str, parent: int):
        if parent == 0:
            return Manifest(()), None
        row = revision_row(connection, collection, parent)
        return parse_stored(row["manifest"], row["manifest_hash"]), row

    def _claim_existing(self, connection: Connection, candidate: str,
                       parts: dict[str, int], now: int) -> int:
        count = 0
        for digest, size in parts.items():
            item = connection.execute(
                "SELECT size FROM objects WHERE digest=? AND sweep_state='live'", (digest,),
            ).fetchone()
            if item is None or active(connection, digest) is not None or item["size"] != size:
                continue
            try:
                self.media.verify(digest, size)
            except VaultError as error:
                report(connection, error, now)
                continue
            verified(connection, digest, now)
            connection.execute(
                "INSERT OR IGNORE INTO candidate_received_parts(candidate,digest,received_at) "
                "VALUES(?,?,?)", (candidate, digest, now),
            )
            count += 1
        return count

    def _claim_base_dependencies(self, connection: Connection, candidate: str,
                                base_manifest: Manifest) -> None:
        for digest, size in base_manifest.unique_parts().items():
            connection.execute(
                "INSERT OR IGNORE INTO candidate_dependencies(candidate,digest,size) "
                "VALUES(?,?,?)", (candidate, digest, size),
            )

    # ---- 查询 -----------------------------------------------------------
    def get(self, candidate: str) -> dict:
        with self.db.transaction(write=False) as connection:
            return summary(connection, candidate_row(connection, candidate),
                           self.db.now(), include_manifest=True)

    def pending_parts(self, candidate: str) -> list[dict]:
        with self.db.transaction(write=False) as connection:
            candidate_row(connection, candidate)
            return pending_parts(connection, candidate)

    def pending(self, candidate: str) -> list[dict]:
        return self.pending_parts(candidate)

    show = get
    info = get
    status = get
    detail = get
    retrieve = get
    find = get

    def list_candidates(self, collection: str | None = None, *, status: str | None = None,
                       after: str = "", limit: int = 50) -> dict:
        integer(limit, 1, 200, "candidate page limit")
        with self.db.transaction(write=False) as connection:
            now = self.db.now()
            if collection is not None:
                collection_id(collection)
                collection_row(connection, collection)
            query = "SELECT * FROM candidates"
            clauses, params = [], []
            if collection is not None:
                clauses.append("collection=?")
                params.append(collection)
            if after:
                clauses.append("id>?")
                params.append(after)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY id LIMIT ?"
            params.append(limit + 1)
            rows = connection.execute(query, params).fetchall()
            items = [summary(connection, row, now) for row in rows[:limit]]
            if status is not None:
                items = [item for item in items if item["status"] == status]
            return {"items": items,
                    "next_after": rows[limit - 1]["id"] if len(rows) > limit else None}

    # 常见近义命名。
    submit = open
    create = open
    register = open
    add = open

    def list(self, *args, **kwargs):
        return self.list_candidates(*args, **kwargs)

    list_all = list_candidates
    browse = list_candidates
    queue = list_candidates

    # ---- 分片补送 -------------------------------------------------------
    def required_size(self, candidate: str, digest: str) -> int:
        from strata.transfer.manifest import sha256
        sha256(digest)
        with self.db.transaction(write=False) as connection:
            require_open(candidate_row(connection, candidate), self.db.now())
            row = connection.execute(
                "SELECT size FROM candidate_expected_parts WHERE candidate=? AND digest=?",
                (candidate, digest),
            ).fetchone()
            if row is None:
                raise missing("expected part", digest)
            return row["size"]

    def receive(self, candidate: str, digest: str, source: BinaryIO) -> dict:
        size = self.required_size(candidate, digest)
        self.media.validate_upload(digest, size, source)
        with self.db.transaction() as connection:
            now = self.db.now()
            row = candidate_row(connection, candidate)
            require_open(row, now)
            obj = connection.execute(
                "SELECT size,sweep_state FROM objects WHERE digest=?", (digest,)).fetchone()
            if obj is not None and obj["size"] != size:
                raise conflict("Known object length differs from declared length", digest=digest)
            if obj is not None and obj["sweep_state"] == "marked":
                raise conflict("Part is being reclaimed, finish sweep before retrying", digest=digest)
            installed = False
            if obj is not None:
                try:
                    self.media.verify(digest, size)
                except VaultError as error:
                    report(connection, error, now)
                    self.media.install(digest, size, source)
                    installed = True
            else:
                self.media.install(digest, size, source)
                connection.execute(
                    "INSERT INTO objects(digest,size,added_at,verified_at) VALUES(?,?,?,?)",
                    (digest, size, now, now),
                )
                installed = True
            verified(connection, digest, now)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO candidate_received_parts(candidate,digest,received_at) "
                "VALUES(?,?,?)", (candidate, digest, now),
            )
            if cursor.rowcount:
                append(connection, row["collection"], "candidate_part_received", candidate, now,
                       digest=digest, size=size, stored=installed)
            return {"candidate": candidate, "id": candidate, "deposit": candidate,
                    "digest": digest, "size": size,
                    "duplicate": cursor.rowcount == 0, "stored": installed}

    upload = receive
    put_part = receive

    # ---- 合订相关转发，支持 vault.candidates.plan(...) 一类调用 ----------
    def _merges(self):
        from strata.collab.merges import Merges
        return Merges(self.db, self.media)

    def plan(self, candidate: str, *args, **kwargs):
        return self._merges().plan(candidate, *args, **kwargs)

    def decide(self, candidate: str, decisions=None, *args, **kwargs):
        return self._merges().decide(candidate, decisions=decisions, *args, **kwargs)

    resolve = decide

    def reconfirm(self, candidate: str, *args, **kwargs):
        return self._merges().reconfirm(candidate, *args, **kwargs)

    def publish_merge(self, candidate: str, *args, **kwargs):
        return self._merges().publish(candidate, *args, **kwargs)

    publish = publish_merge
    bind = publish_merge
    combine = publish_merge

    def merge(self, candidate: str, *args, **kwargs):
        return self._merges().publish(candidate, *args, **kwargs)

    # ---- 终止：撤销 / 结束 / 到期 --------------------------------------
    def revoke(self, candidate: str, *, reason: str | None = None) -> dict:
        return self._terminate(candidate, "revoked", "candidate_revoked", reason=reason)

    def close(self, candidate: str, *, reason: str | None = None) -> dict:
        return self._terminate(candidate, "closed", "candidate_closed", reason=reason)

    cancel = revoke
    withdraw = revoke
    finish = close
    conclude = close

    def _terminate(self, candidate: str, target: str, event_kind: str, *,
                   reason: str | None = None) -> dict:
        with self.db.transaction() as connection:
            now = self.db.now()
            row = candidate_row(connection, candidate)
            state = effective_state(row, now)
            if row["status"] in ("revoked", "closed", "expired", "published"):
                return summary(connection, row, now)
            if state != "open":
                # 自然到期但尚未执行 expire：直接落到 expired。
                target = "expired"
                event_kind = "candidate_expired"
            connection.execute(
                "UPDATE candidates SET status=?,concluded_at=? WHERE id=?",
                (target, now, candidate),
            )
            self._drop_claims(connection, candidate)
            append(connection, row["collection"], event_kind, candidate, now, reason=reason)
            return summary(connection, candidate_row(connection, candidate), now)

    @staticmethod
    def _drop_claims(connection: Connection, candidate: str) -> None:
        connection.execute("DELETE FROM candidate_received_parts WHERE candidate=?", (candidate,))
        connection.execute("DELETE FROM candidate_dependencies WHERE candidate=?", (candidate,))

    def expire(self, limit: int = 100) -> list[str]:
        integer(limit, 1, 500, "expiration limit")
        with self.db.transaction() as connection:
            now = self.db.now()
            rows = connection.execute(
                "SELECT id,collection FROM candidates WHERE status='open' AND expires_at<=? "
                "ORDER BY expires_at,id LIMIT ?", (now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE candidates SET status='expired',concluded_at=? WHERE id=?",
                    (now, row["id"]),
                )
                self._drop_claims(connection, row["id"])
                append(connection, row["collection"], "candidate_expired", row["id"], now)
            return [row["id"] for row in rows]

    # ---- 清单与变化的内部读取 -------------------------------------------
    @staticmethod
    def load_manifest(row) -> Manifest:
        return parse_stored(row["manifest"], row["manifest_hash"])

    @staticmethod
    def load_changes(row) -> ChangeSet:
        return parse_changes_json(row["changes"], row["changes_hash"])


# 近义类名。
CandidateService = Candidates
CorrectionCandidateService = Candidates
Corrections = Candidates
