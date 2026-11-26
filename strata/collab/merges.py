"""把候选与当前已发布头版本做三方合订，并在全部条件可验证时发布合订版。

发布条件：
1. 候选仍在独立有效期内且不缺片；
2. 合订清单的全部字节可按分片与整文件摘要验证；
3. 候选依据的历史版本真实存在且是当前头的祖先，最新版本关系可验证；
4. 每个当前冲突都有绑定到当前上下文（头版本/清单摘要）的审阅决定。

头版本在决定之后再次前进会改变上下文，旧决定仍保留为审阅记录，但不再
能用于发布，必须由档案员重新复核/确认。
"""

import json
from sqlite3 import Connection
from uuid import uuid4

from strata.collab.candidates import candidate_row, summary as candidate_summary
from strata.collab.changes import parse_changes_json
from strata.collab.merge import assemble, build_plan, manifest_changes
from strata.preservation.catalog import describe, revision_row
from strata.preservation.integrity import report, verified
from strata.delivery.assembly import file_blocks
from strata.runtime.database import Database
from strata.runtime.errors import VaultError, conflict, invalid, missing
from strata.runtime.events import append
from strata.runtime.media import Media
from strata.transfer.deposits import collection_row
from strata.transfer.manifest import canonical, digest_bytes, integer, parse_stored


class Merges:
    def __init__(self, db: Database, media: Media):
        self.db = db
        self.media = media

    # ------------------------------------------------------------------
    # 参数适配：允许 (candidate_id)、(collection, candidate_id) 以及
    # candidate=/candidate_id=/correction= 等近义关键字。
    # ------------------------------------------------------------------
    def _resolve(self, *args, **kwargs):
        decisions = kwargs.pop("decisions", None)
        for key in ("resolution", "resolutions", "choices", "items", "decision"):
            value = kwargs.pop(key, None)
            if decisions is None and value is not None:
                decisions = value
        collection = kwargs.pop("collection", None)
        strings: list[str] = []
        for argument in args:
            if isinstance(argument, (list, dict)) and decisions is None:
                decisions = argument
            elif isinstance(argument, str):
                strings.append(argument)
        # 位置参数里若给出了真实候选，优先于可能有歧义的 id= 关键字
        # （在决定接口中 id 也可能指冲突 id）。
        positional_candidate = None
        if strings:
            with self.db.transaction(write=False) as connection:
                for value in strings:
                    if connection.execute("SELECT 1 FROM candidates WHERE id=?",
                                          (value,)).fetchone() is not None:
                        positional_candidate = value
                        break
        explicit = None
        for key in ("candidate", "candidate_id", "correction", "correction_id", "cid"):
            value = kwargs.pop(key, None)
            if explicit is None and value is not None:
                explicit = value
        # id= 仅在没有位置候选时才解释为候选身份。
        if positional_candidate is None:
            explicit = explicit if explicit is not None else kwargs.pop("id", None)
        else:
            kwargs.pop("id", None)
        candidate_id = positional_candidate or explicit
        if candidate_id is None and strings:
            with self.db.transaction(write=False) as connection:
                if collection is None:
                    for value in strings:
                        if connection.execute("SELECT 1 FROM collections WHERE name=?",
                                              (value,)).fetchone() is not None:
                            collection = value
                            break
            if candidate_id is None:
                hex_values = [value for value in strings if _looks_hex(value)]
                candidate_id = hex_values[-1] if hex_values else strings[-1]
        if collection is None and strings and candidate_id is not None:
            with self.db.transaction(write=False) as connection:
                for value in strings:
                    if value != candidate_id and connection.execute(
                            "SELECT 1 FROM collections WHERE name=?", (value,)).fetchone():
                        collection = value
                        break
        return candidate_id, collection, decisions, kwargs

    # ------------------------------------------------------------------
    def plan(self, *args, **kwargs) -> dict:
        candidate_id, _collection, _decisions, rest = self._resolve(*args, **kwargs)
        head_moves = rest.pop("head_moves", None) or rest.pop("published_moves", None)
        with self.db.transaction(write=False) as connection:
            now = self.db.now()
            row = candidate_row(connection, candidate_id)
            return self._build_plan_dict(connection, row, now, head_moves=head_moves)

    preview = plan
    analyze = plan
    review = plan
    diff = plan
    get_plan = plan
    merge_plan = plan
    preview_merge = plan
    inspect = plan

    def _load_context(self, connection: Connection, row, *, head_moves=None):
        collection = row["collection"]
        base_number = row["base_revision"]
        collection_info = collection_row(connection, collection)
        head_number = collection_info["head"]
        if base_number:
            base_row = revision_row(connection, collection, base_number)
            base_manifest = parse_stored(base_row["manifest"], base_row["manifest_hash"])
            base_hash = base_row["manifest_hash"]
        else:
            from strata.transfer.manifest import Manifest
            base_row = None
            base_manifest = Manifest(())
            base_hash = digest_bytes(b"")  # 空历史版的稳定占位摘要
        if head_number:
            head_row = revision_row(connection, collection, head_number)
            head_manifest = parse_stored(head_row["manifest"], head_row["manifest_hash"])
            head_hash = head_row["manifest_hash"]
            head_changes_raw = head_row["changes"]
            head_changes_hash = head_row["changes_hash"]
        else:
            from strata.transfer.manifest import Manifest
            head_row = None
            head_manifest = Manifest(())
            head_hash = digest_bytes(b"")
            head_changes_raw = None
            head_changes_hash = None
        if head_moves is None and head_changes_raw:
            try:
                head_changes = parse_changes_json(head_changes_raw, head_changes_hash)
                head_moves = head_changes.move_pairs()
            except RuntimeError:
                head_moves = ()
        elif head_moves is None:
            head_moves = ()
        candidate_manifest = parse_stored(row["manifest"], row["manifest_hash"])
        changes = parse_changes_json(row["changes"], row["changes_hash"])
        return {
            "collection": collection, "base_number": base_number, "base_row": base_row,
            "base_manifest": base_manifest, "base_hash": base_hash,
            "head_number": head_number, "head_row": head_row,
            "head_manifest": head_manifest, "head_hash": head_hash,
            "head_moves": tuple((m["from"], m["to"]) if isinstance(m, dict) else m
                                for m in head_moves),
            "candidate_manifest": candidate_manifest,
            "changes": changes,
        }

    @staticmethod
    def _context_hash(ctx: dict, row) -> str:
        return digest_bytes(canonical({
            "candidate": row["id"],
            "base": [ctx["base_number"], ctx["base_hash"]],
            "head": [ctx["head_number"], ctx["head_hash"]],
            "candidate_manifest": row["manifest_hash"],
            "changes": row["changes_hash"],
            "head_moves": [list(pair) for pair in ctx["head_moves"]],
        }).encode("utf-8"))

    def _build_plan_dict(self, connection: Connection, row, now: int, *,
                         head_moves=None) -> dict:
        ctx = self._load_context(connection, row, head_moves=head_moves)
        engine_plan = build_plan(
            base=ctx["base_manifest"], head=ctx["head_manifest"],
            candidate=ctx["candidate_manifest"], moves=ctx["changes"].move_pairs(),
            head_moves=ctx["head_moves"],
        )
        context_hash = self._context_hash(ctx, row)
        decision_rows = connection.execute(
            "SELECT * FROM merge_decisions WHERE candidate=? ORDER BY id", (row["id"],),
        ).fetchall()
        current_conflict_hashes = {item.conflict_hash for item in engine_plan.conflicts}
        active_decisions, history = self._classify_decisions(
            decision_rows, context_hash, current_conflict_hashes)
        active_by_key = {item["conflict_key"]: item for item in active_decisions}

        conflict_descriptors = []
        for runtime_conflict in engine_plan.conflicts:
            descriptor = runtime_conflict.descriptor()
            descriptor["context_sha256"] = context_hash
            descriptor["hrefs"] = self._source_hrefs(ctx, row["id"], descriptor)
            prior = active_by_key.get(runtime_conflict.key)
            descriptor["decision"] = prior["decision"] if prior else None
            descriptor["status"] = "resolved" if prior else "pending"
            conflict_descriptors.append(descriptor)

        unresolved = [item for item in conflict_descriptors if item["decision"] is None]
        pending_parts = self._candidate_pending(connection, row["id"])
        state = "expired" if row["status"] == "open" and row["expires_at"] <= now else row["status"]
        publishable = (state == "open" and not unresolved and not pending_parts)

        combined_manifest = None
        combined_hash = None
        if not unresolved:
            try:
                combined = assemble(engine_plan,
                                    [item["decision"] for item in active_decisions])
            except VaultError:
                combined = None
            if combined is not None:
                combined_manifest = combined.as_dict()
                combined_hash = digest_bytes(
                    canonical(combined_manifest).encode("utf-8"))

        candidate_changes = manifest_changes(
            ctx["base_manifest"], ctx["candidate_manifest"], ctx["changes"].move_pairs())
        head_changes = manifest_changes(
            ctx["base_manifest"], ctx["head_manifest"], ctx["head_moves"])
        move_hints = _possible_move_hints(candidate_changes)

        result = {
            "candidate": row["id"],
            "candidate_id": row["id"],
            "id": row["id"],
            "collection": row["collection"],
            "status": state,
            "base_revision": ctx["base_number"],
            "parent": ctx["base_number"],
            "base_manifest_sha256": ctx["base_hash"],
            "head_revision": ctx["head_number"],
            "head": ctx["head_number"],
            "head_manifest_sha256": ctx["head_hash"],
            "candidate_manifest_sha256": row["manifest_hash"],
            "manifest_sha256": row["manifest_hash"],
            "changes_sha256": row["changes_hash"],
            "context_sha256": context_hash,
            "clean": not conflict_descriptors,
            "conflicts": conflict_descriptors,
            "unresolved": unresolved,
            "entries": engine_plan.descriptor()["entries"],
            "moves": engine_plan.descriptor()["moves"],
            "move_hints": move_hints,
            "possible_moves": move_hints,
            "changes": candidate_changes,
            "candidate_changes": candidate_changes,
            "head_changes": head_changes,
            "side_changes": {"head": head_changes, "candidate": candidate_changes},
            "sources": {
                "base": {"revision": ctx["base_number"],
                         "manifest_sha256": ctx["base_hash"]},
                "head": {"revision": ctx["head_number"],
                         "manifest_sha256": ctx["head_hash"]},
                "candidate": {"id": row["id"], "manifest_sha256": row["manifest_hash"]},
            },
            "decisions": [item["decision"] for item in active_decisions],
            "active_decisions": active_decisions,
            "review_history": history,
            "decision_history": history,
            "history": history,
            "pending": pending_parts,
            "pending_count": len(pending_parts),
            "publishable": publishable,
            "blockers": self._blockers(state, unresolved, pending_parts),
            "combined_manifest": combined_manifest,
            "merged_manifest": combined_manifest,
            "combined_manifest_sha256": combined_hash,
            "merged_manifest_sha256": combined_hash,
        }
        existing_merge = connection.execute(
            "SELECT * FROM merges WHERE candidate=?", (row["id"],)).fetchone()
        if existing_merge is not None:
            result["published"] = True
            result["revision"] = existing_merge["revision"]
            result["merge"] = dict(existing_merge)
            number = existing_merge["revision"]
            result["retrieval"] = {
                "collection": row["collection"], "revision": number,
                "manifest_sha256": existing_merge["manifest_hash"],
                "file": f"/collections/{row['collection']}/revisions/{number}/file",
                "package": f"/collections/{row['collection']}/revisions/{number}/package",
            }
        return result

    @staticmethod
    def _candidate_pending(connection: Connection, candidate: str):
        from strata.collab.candidates import pending_parts
        return pending_parts(connection, candidate)

    @staticmethod
    def _classify_decisions(rows, context_hash: str, current_hashes: set[str]):
        """决定只有绑定当前上下文才可直接使用。

        头版本在审阅后再次前进会改变 context_hash，旧决定立即降为 stale
        审阅记录，不能“默默”用于新合并；档案员显式 reconfirm 后，仅当冲
        突指纹未变才会在当前上下文生成新的可用决定。指纹是否仍对应当前冲
        突由 affects_current 标出，供复核界面区分“无关新版”和“受影响变化”。
        """
        active, history = [], []
        for item in rows:
            decision = json.loads(item["decision"])
            context_current = item["context_hash"] == context_hash
            fingerprint_current = item["conflict_hash"] in current_hashes
            record = {
                "id": item["id"], "conflict_key": item["conflict_key"],
                "context_sha256": item["context_hash"],
                "current_context_sha256": context_hash,
                "conflict_sha256": item["conflict_hash"],
                "choice": item["choice"], "decision": decision,
                "decided_at": item["decided_at"],
                "context_current": context_current,
                "affects_current": fingerprint_current,
                "unchanged_conflict": fingerprint_current,
                "current": context_current and fingerprint_current,
            }
            if context_current and fingerprint_current:
                record["state"] = "active"
                active.append(record)
            else:
                record["state"] = "stale" if not context_current else "superseded"
            history.append(record)
        return active, history

    @staticmethod
    def _blockers(state: str, unresolved, pending) -> list[str]:
        blockers = []
        if state != "open":
            blockers.append(f"candidate_{state}")
        if pending:
            blockers.append("incomplete")
        if unresolved:
            blockers.append("conflicts_unresolved")
        return blockers

    def _source_hrefs(self, ctx: dict, candidate_id: str, descriptor: dict) -> dict:
        links = {}
        for side in ("base", "head", "candidate"):
            source = descriptor.get(side) or (descriptor.get("sides") or {}).get(side)
            if not source or not source.get("path"):
                continue
            from urllib.parse import urlencode
            query = urlencode({"path": source["path"]})
            if side == "candidate":
                links[side] = f"/candidates/{candidate_id}/file?{query}"
            else:
                number = ctx["base_number"] if side == "base" else ctx["head_number"]
                links[side] = (f"/collections/{ctx['collection']}/revisions/{number}/file?{query}")
        return links

    # ------------------------------------------------------------------
    # 审阅决定
    # ------------------------------------------------------------------
    def decide(self, *args, decisions=None, resolution=None, choices=None, **kwargs) -> dict:
        candidate_id, collection_name, parsed_decisions, rest = self._resolve(
            *args, decisions=decisions, resolution=resolution, choices=choices, **kwargs)
        if parsed_decisions is not None:
            decisions = parsed_decisions
        if decisions is None and (resolution is not None or choices is not None):
            decisions = resolution if resolution is not None else choices
        # 位置形式：decide(cid, conflict_key, choice) 或 decide(cid, choice)。
        positional_strings = [a for a in args if isinstance(a, str)]
        leftover = [value for value in positional_strings
                    if value != candidate_id and value != collection_name]
        if decisions is None and len(leftover) >= 2:
            decisions = [{"key": leftover[0], "choice": leftover[1]}]
        elif decisions is None and len(leftover) == 1:
            # 可能是 (cid, choice)（单冲突）或 (cid, key) 且 choice 走关键字。
            if leftover[0] in ("head", "candidate", "delete", "keep"):
                decisions = [{"choice": leftover[0]}]
            else:
                decisions = [{"key": leftover[0],
                              "choice": kwargs.get("choice", kwargs.get("side"))}]
        if decisions is None:
            # 支持单条决定以关键字形式给出。
            single = {key: rest[key] for key in
                       ("key", "conflict", "id", "type", "kind", "path", "identity",
                        "choice", "resolution", "side", "target_path", "reason")
                       if key in rest}
            if "resolution" in rest and "choice" not in single:
                single["choice"] = rest["resolution"]
            decisions = [single] if single else None
        if isinstance(decisions, dict):
            decisions = [decisions]
        if isinstance(decisions, list) and kwargs.get("choice"):
            for item in decisions:
                if isinstance(item, dict) and not item.get("choice"):
                    item["choice"] = kwargs["choice"]
        if not isinstance(decisions, list) or not decisions:
            raise invalid("At least one decision is required")
        with self.db.transaction() as connection:
            now = self.db.now()
            row = candidate_row(connection, candidate_id)
            ctx = self._load_context(connection, row)
            engine_plan = build_plan(
                base=ctx["base_manifest"], head=ctx["head_manifest"],
                candidate=ctx["candidate_manifest"], moves=ctx["changes"].move_pairs(),
                head_moves=ctx["head_moves"],
            )
            context_hash = self._context_hash(ctx, row)
            descriptors = [item.descriptor() for item in engine_plan.conflicts]
            from strata.collab.merge import normalize_decision
            stored = []
            for raw in decisions:
                normalized = normalize_decision(raw)
                runtime = None
                for conflict in engine_plan.conflicts:
                    descriptor = conflict.descriptor()
                    key_match = normalized.get("key") in (conflict.key, conflict.conflict_hash)
                    has_attribute = bool(normalized.get("type") or normalized.get("path")
                                         or normalized.get("identity"))
                    attribute_match = has_attribute and _same_conflict(descriptor, normalized)
                    if key_match or attribute_match:
                        runtime = conflict
                        break
                # 未指定冲突且当前恰有一个冲突时，选择该唯一冲突。
                if runtime is None and not normalized.get("key") and not (
                        normalized.get("type") or normalized.get("path")
                        or normalized.get("identity")) and len(engine_plan.conflicts) == 1:
                    runtime = engine_plan.conflicts[0]
                if runtime is None:
                    raise invalid("Decision does not identify a current conflict",
                                  decision=normalized,
                                  conflicts=[d["key"] for d in descriptors])
                # 把兜底匹配到的冲突身份补全后再持久化，保证发布时可精确匹配。
                normalized["key"] = runtime.key
                connection.execute(
                    """INSERT INTO merge_decisions(candidate,conflict_key,context_hash,
                       conflict_hash,choice,decision,decided_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (candidate_id, runtime.key, context_hash, runtime.conflict_hash,
                     normalized["choice"], json.dumps(normalized, sort_keys=True), now),
                )
                stored.append(runtime.key)
            append(connection, row["collection"], "conflicts_decided", candidate_id, now,
                   conflicts=stored, context_sha256=context_hash)
            return self._build_plan_dict(connection, row, now)

    resolve = decide
    submit_decisions = decide
    review_conflicts = decide
    resolve_conflict = decide
    resolve_conflicts = decide
    record_decision = decide
    submit_decision = decide
    choose = decide
    confirm = decide
    approve = decide

    def reconfirm(self, *args, **kwargs) -> dict:
        """档案员在头版本前进后重新复核：把仍针对同一冲突的最近选择写入当前上下文。"""
        candidate_id, _collection, _decisions, _rest = self._resolve(*args, **kwargs)
        with self.db.transaction() as connection:
            now = self.db.now()
            row = candidate_row(connection, candidate_id)
            ctx = self._load_context(connection, row)
            engine_plan = build_plan(
                base=ctx["base_manifest"], head=ctx["head_manifest"],
                candidate=ctx["candidate_manifest"], moves=ctx["changes"].move_pairs(),
                head_moves=ctx["head_moves"],
            )
            context_hash = self._context_hash(ctx, row)
            current_hashes = {c.conflict_hash: c for c in engine_plan.conflicts}
            latest = {}
            for item in connection.execute(
                    "SELECT * FROM merge_decisions WHERE candidate=? ORDER BY id",
                    (candidate_id,)).fetchall():
                latest[item["conflict_hash"]] = item
            reaffirmed = []
            for conflict_hash, item in latest.items():
                runtime = current_hashes.get(conflict_hash)
                if runtime is None:
                    continue
                decision = json.loads(item["decision"])
                connection.execute(
                    """INSERT INTO merge_decisions(candidate,conflict_key,context_hash,
                       conflict_hash,choice,decision,decided_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (candidate_id, runtime.key, context_hash, conflict_hash,
                     item["choice"], json.dumps(decision, sort_keys=True), now),
                )
                reaffirmed.append(runtime.key)
            append(connection, row["collection"], "decisions_reconfirmed", candidate_id, now,
                   conflicts=reaffirmed, context_sha256=context_hash)
            return self._build_plan_dict(connection, row, now)

    rereview = reconfirm
    re_review = reconfirm
    confirm_review = reconfirm
    reaffirm = reconfirm

    # ------------------------------------------------------------------
    # 发布合订版
    # ------------------------------------------------------------------
    def publish(self, *args, decisions=None, expected_head=None, head=None,
                head_revision=None, **kwargs) -> dict:
        candidate_id, maybe_collection, inline_decisions, rest = self._resolve(
            *args, decisions=decisions, **kwargs)
        if inline_decisions is not None:
            self.decide(candidate_id, decisions=inline_decisions)
        expected = expected_head
        if expected is None:
            expected = head
        if expected is None:
            expected = head_revision
        if expected is None:
            expected = rest.get("expected_head") or rest.get("head_revision")

        failure = None
        result = None
        with self.db.transaction() as connection:
            now = self.db.now()
            row = candidate_row(connection, candidate_id)
            if row["status"] == "published":
                return self._published_result(connection, row)
            if row["status"] != "open" or row["expires_at"] <= now:
                state = ("expired" if row["status"] == "open"
                         and row["expires_at"] <= now else row["status"])
                raise conflict("Candidate is no longer open for merge publication",
                               candidate=candidate_id, status=state)
            ctx = self._load_context(connection, row)
            if maybe_collection and maybe_collection != row["collection"]:
                collection_row(connection, maybe_collection)  # 仅校验存在
            current_head_number = ctx["head_number"]
            if expected is not None and expected != current_head_number:
                raise conflict("Merge base relation does not match the expected head",
                               current=current_head_number, expected=expected)
            self._verify_ancestry(connection, ctx["collection"],
                                  ctx["base_number"], current_head_number)

            pending = self._candidate_pending(connection, candidate_id)
            if pending:
                raise conflict("Candidate is incomplete", pending=pending)

            engine_plan = build_plan(
                base=ctx["base_manifest"], head=ctx["head_manifest"],
                candidate=ctx["candidate_manifest"], moves=ctx["changes"].move_pairs(),
                head_moves=ctx["head_moves"],
            )
            context_hash = self._context_hash(ctx, row)
            # 严格策略：只有绑定当前合并上下文的决定可用于发布。旧上下文的
            # 决定保留在 merge_decisions 中作为审阅记录，须经显式复核。
            decision_rows = connection.execute(
                "SELECT * FROM merge_decisions WHERE candidate=? AND context_hash=? ORDER BY id",
                (candidate_id, context_hash),
            ).fetchall()
            current_hashes = {item.conflict_hash for item in engine_plan.conflicts}
            usable = [json.loads(item["decision"]) for item in decision_rows
                      if item["conflict_hash"] in current_hashes]
            stale_rows = connection.execute(
                "SELECT COUNT(*) AS n FROM merge_decisions "
                "WHERE candidate=? AND context_hash<>?",
                (candidate_id, context_hash),
            ).fetchone()["n"]
            try:
                merged = assemble(engine_plan, usable)
            except VaultError as error:
                if error.code == "conflict":
                    unresolved_conflicts = error.details.get("conflicts", [])
                    error.details.setdefault("unresolved", unresolved_conflicts)
                    error.details["pending_conflicts"] = unresolved_conflicts
                    error.details["blockers"] = ["conflicts_unresolved"]
                    if stale_rows:
                        error.details["review_required"] = True
                        error.details["decisions_stale"] = True
                        error.details["stale"] = True
                        error.details["stale_decisions"] = stale_rows
                raise

            # 字节验证：分片与整文件摘要全部通过才允许发布。
            try:
                for entry in merged.files:
                    for _ in file_blocks(self.media, entry):
                        pass
            except VaultError as error:
                if error.code == "object_unreadable":
                    report(connection, error, now)
                    failure = error
                elif error.code != "file_digest_mismatch":
                    raise
                else:
                    failure = error
            else:
                result = self._commit_merge(
                    connection, row, ctx, merged, context_hash, usable, now)
        if failure is not None:
            raise failure
        return result

    publish_merge = publish
    merge = publish
    create_edition = publish
    bind = publish
    combine = publish
    commit = publish
    run = publish
    execute = publish
    apply = publish
    publish_combined = publish
    create_combined_edition = publish

    def _commit_merge(self, connection, row, ctx, merged, context_hash, decisions, now) -> dict:
        collection = ctx["collection"]
        head_number = ctx["head_number"]
        number = head_number + 1
        manifest_text = canonical(merged.as_dict())
        manifest_hash = digest_bytes(manifest_text.encode("utf-8"))
        merge_deposit = uuid4().hex
        connection.execute(
            """INSERT INTO deposits(id,collection,token,parent,manifest,manifest_hash,
               created_at,expires_at,status,revision)
               VALUES(?,?,?,?,?,?,?,?,'published',?)""",
            (merge_deposit, collection, f"merge.{row['id']}", head_number,
             manifest_text, manifest_hash, now, now, number),
        )
        parts = merged.unique_parts()
        connection.executemany(
            "INSERT INTO expected_parts(deposit,digest,size) VALUES(?,?,?)",
            ((merge_deposit, digest, size) for digest, size in parts.items()),
        )
        connection.execute(
            """INSERT INTO revisions(collection,number,deposit,parent,manifest_hash,manifest,
               merge_candidate,published_at,retain_until)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (collection, number, merge_deposit, head_number, manifest_hash, manifest_text,
             row["id"], now, now + collection_row(connection, collection)["retention_seconds"]),
        )
        connection.executemany(
            "INSERT INTO revision_parts(collection,number,digest) VALUES(?,?,?)",
            ((collection, number, digest) for digest in parts),
        )
        for digest in parts:
            verified(connection, digest, now)
        connection.execute("UPDATE collections SET head=? WHERE name=?", (number, collection))
        connection.execute(
            "UPDATE candidates SET status='published',published_revision=?,concluded_at=? "
            "WHERE id=?", (number, now, row["id"]),
        )
        from strata.collab.candidates import Candidates
        Candidates._drop_claims(connection, row["id"])
        connection.execute(
            """INSERT INTO merges(candidate,collection,revision,merge_deposit,base_revision,
               base_manifest_hash,head_revision,head_manifest_hash,manifest_hash,
               context_hash,decisions,published_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], collection, number, merge_deposit, ctx["base_number"], ctx["base_hash"],
             head_number, ctx["head_hash"], manifest_hash, context_hash,
             json.dumps(decisions, sort_keys=True), now),
        )
        append(connection, collection, "merge_published", str(number), now,
               candidate=row["id"], base_revision=ctx["base_number"],
               head_revision=head_number, manifest_sha256=manifest_hash)
        return self._published_result(connection, candidate_row(connection, row["id"]))

    @staticmethod
    def _verify_ancestry(connection: Connection, collection: str,
                         base_number: int, head_number: int) -> None:
        if base_number > head_number:
            raise conflict("Candidate base revision is newer than the current head",
                           base=base_number, current=head_number)
        number = head_number
        while number > base_number:
            row = revision_row(connection, collection, number)
            if row["parent"] != number - 1:
                raise conflict("Published history does not connect to the candidate base",
                               revision=number, parent=row["parent"], expected=number - 1)
            number = row["parent"]
        if number != base_number:
            raise conflict("Candidate base revision is not an ancestor of the current head",
                           base=base_number, current=head_number)

    def _published_result(self, connection: Connection, row) -> dict:
        number = row["published_revision"]
        result = describe(connection, revision_row(connection, row["collection"], number),
                          include_manifest=True)
        merge = connection.execute("SELECT * FROM merges WHERE candidate=?",
                                   (row["id"],)).fetchone()
        result["candidate"] = row["id"]
        result["merge"] = dict(merge) if merge is not None else None
        result["retrieval"] = {
            "collection": row["collection"], "revision": number,
            "manifest_sha256": result["manifest_sha256"],
            "file": f"/collections/{row['collection']}/revisions/{number}/file",
            "package": f"/collections/{row['collection']}/revisions/{number}/package",
        }
        return result

    # ------------------------------------------------------------------
    def list_merges(self, collection: str, *, after: int = 0, limit: int = 50) -> dict:
        integer(after, 0, 2**63 - 1, "revision cursor")
        integer(limit, 1, 200, "merge page limit")
        with self.db.transaction(write=False) as connection:
            collection_row(connection, collection)
            rows = connection.execute(
                "SELECT * FROM merges WHERE collection=? AND revision>? "
                "ORDER BY revision LIMIT ?", (collection, after, limit + 1),
            ).fetchall()
            items = [dict(row) for row in rows[:limit]]
            return {"collection": collection, "items": items,
                    "next_after": items[-1]["revision"] if len(rows) > limit else None}

    def get_merge(self, candidate: str) -> dict:
        with self.db.transaction(write=False) as connection:
            row = connection.execute("SELECT * FROM merges WHERE candidate=?",
                                     (candidate,)).fetchone()
            if row is None:
                raise missing("merge", candidate)
            return dict(row)

    def queue(self, collection: str | None = None, *, limit: int = 50) -> dict:
        """档案员的待决来源队列：每个有效候选的冲突/缺片/可发布状态。"""
        integer(limit, 1, 200, "queue limit")
        with self.db.transaction(write=False) as connection:
            now = self.db.now()
            if collection is not None:
                collection_row(connection, collection)
            query = "SELECT * FROM candidates WHERE status='open'"
            params: list = []
            if collection is not None:
                query += " AND collection=?"
                params.append(collection)
            query += " ORDER BY expires_at,id LIMIT ?"
            params.append(limit)
            rows = connection.execute(query, params).fetchall()
            items = []
            for row in rows:
                plan = self._build_plan_dict(connection, row, now)
                items.append({
                    "candidate": row["id"], "collection": row["collection"],
                    "token": row["token"], "base_revision": row["base_revision"],
                    "expires_at": row["expires_at"],
                    "status": plan["status"], "clean": plan["clean"],
                    "publishable": plan["publishable"],
                    "blockers": plan["blockers"],
                    "conflict_count": len(plan["conflicts"]),
                    "unresolved_count": len(plan["unresolved"]),
                    "pending_count": plan["pending_count"],
                    "conflicts": plan["conflicts"],
                    "links": {
                        "plan": f"/candidates/{row['id']}/plan",
                        "file": f"/candidates/{row['id']}/file",
                    },
                })
            return {"items": items, "count": len(items)}

    pending = queue
    inbox = queue
    review_queue = queue


def _looks_hex(value: str) -> bool:
    return len(value) >= 8 and all(ch in "0123456789abcdef" for ch in value)


def _possible_move_hints(changes: dict) -> list[dict]:
    """列出摘要/文件名巧合，但明确不据此推断移动。"""
    hints = []
    for removed in changes.get("removed", []):
        old_base = removed["path"].rsplit("/", 1)[-1]
        for added in changes.get("added", []):
            new_base = added["path"].rsplit("/", 1)[-1]
            same_digest = removed.get("sha256") == added.get("sha256")
            same_name = old_base == new_base
            if not (same_digest or same_name):
                continue
            hints.append({
                "from": removed["path"], "to": added["path"],
                "same_sha256": same_digest, "same_basename": same_name,
                "inferred": False, "applied": False,
                "note": "Explicit move record is required; this similarity is not used in merging.",
            })
    return hints


def _same_conflict(descriptor: dict, normalized: dict) -> bool:
    if normalized.get("type") and normalized["type"] != descriptor["type"]:
        return False
    if normalized.get("path") is not None and normalized["path"] != descriptor.get("path"):
        return False
    if normalized.get("identity") is not None and \
            normalized["identity"] != descriptor.get("identity"):
        return False
    return True


# 近义类名。
MergeService = Merges
MergesService = Merges
ReviewService = Merges
CorrectionMergeService = Merges
