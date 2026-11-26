"""定义现场接收、更正候选、多方合订、长期保留和完整性取回的接口。"""

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool

from strata.httpio import JsonLimit, download, error_response, uploaded
from strata.runtime.errors import VaultError, invalid
from strata.runtime.vault import Vault


class CollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    retention_seconds: StrictInt


class DepositInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    token: StrictStr
    parent: StrictInt
    lifetime_seconds: StrictInt
    manifest: dict
    changes: object | None = None
    moves: object | None = None
    operations: object | None = None


class HoldInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: StrictStr


def create_app(root: Path, *, clock=None) -> FastAPI:
    vault = Vault(root, clock)
    app = FastAPI(title="Strata Vault", version="2.0.0")
    app.add_middleware(JsonLimit)
    app.state.vault = vault

    @app.exception_handler(VaultError)
    async def business_error(_request: Request, error: VaultError):
        return error_response(error)

    # ------------------------------------------------------------------
    # 馆藏
    # ------------------------------------------------------------------
    @app.post("/collections", status_code=201)
    def create_collection(body: CollectionInput):
        return vault.catalog.create_collection(body.name, body.retention_seconds)

    @app.get("/collections")
    def list_collections(after: str = "", limit: int = 50):
        return vault.catalog.collections(after, limit)

    @app.get("/collections/{collection}")
    def get_collection(collection: str):
        return vault.catalog.collection(collection)

    # ------------------------------------------------------------------
    # 普通线性存款会话（原合同，并允许附带显式变化）
    # ------------------------------------------------------------------
    @app.post("/collections/{collection}/deposits", status_code=201)
    def open_deposit(collection: str, body: DepositInput):
        return vault.deposits.open(
            collection, body.token, body.parent, body.lifetime_seconds, body.manifest,
            changes=body.changes, moves=body.moves, operations=body.operations,
        )

    @app.get("/deposits/{deposit}")
    def get_deposit(deposit: str):
        try:
            return vault.deposits.get(deposit)
        except VaultError as error:
            if error.code != "not_found":
                raise
            # 兼容把更正候选视为同类会话的读取方式。
            return vault.candidates.get(deposit)

    @app.delete("/deposits/{deposit}")
    def cancel_deposit(deposit: str):
        try:
            return vault.deposits.cancel(deposit)
        except VaultError as error:
            if error.code != "not_found":
                raise
            return vault.candidates.revoke(deposit)

    @app.put("/deposits/{deposit}/parts/{digest}")
    async def receive_part(deposit: str, digest: str, request: Request):
        # 存款会话与更正候选都可经同形 URL 续传分片。
        try:
            size = await run_in_threadpool(vault.deposits.required_size, deposit, digest)
            kind = "deposit"
        except VaultError as error:
            if error.code != "not_found":
                raise
            size = await run_in_threadpool(vault.candidates.required_size, deposit, digest)
            kind = "candidate"
        with await uploaded(request, size, vault.delivery.scratch) as source:
            if kind == "deposit":
                return await run_in_threadpool(vault.deposits.receive, deposit, digest, source)
            return await run_in_threadpool(vault.candidates.receive, deposit, digest, source)

    @app.post("/deposits/{deposit}/publish")
    def publish(deposit: str):
        try:
            return vault.catalog.publish(deposit)
        except VaultError as error:
            if error.code != "not_found":
                raise
            # 候选会话的“发布”即复核并发布合订版。
            return vault.merges.publish(deposit)

    # ------------------------------------------------------------------
    # 更正候选：接入、列表、查询、补片、终止
    # ------------------------------------------------------------------
    def _candidate_body(collection: str, body: dict) -> dict:
        token = body.get("token")
        parent = body.get("parent", body.get("base_revision", body.get("base", 0)))
        lifetime = body.get("lifetime_seconds",
                            body.get("lifetime", body.get("ttl_seconds", body.get("expires_in", 604800))))
        manifest = body.get("manifest")
        changes = body.get("changes", body.get("change_set"))
        moves = body.get("moves", body.get("renames"))
        operations = body.get("operations")
        return vault.candidates.open(
            collection, token, parent, lifetime, manifest,
            changes=changes, moves=moves, operations=operations,
        )

    @app.post("/collections/{collection}/candidates", status_code=201)
    async def open_candidate(collection: str, request: Request):
        return _candidate_body(collection, await _json_object(request))

    @app.post("/collections/{collection}/corrections", status_code=201)
    async def open_correction_alias(collection: str, request: Request):
        return _candidate_body(collection, await _json_object(request))

    @app.post("/candidates", status_code=201)
    async def open_candidate_flat(request: Request):
        body = await _json_object(request)
        collection = body.get("collection")
        if not isinstance(collection, str):
            raise invalid("collection is required")
        return _candidate_body(collection, body)

    @app.get("/collections/{collection}/candidates")
    def list_candidates_scoped(collection: str, status: str | None = None,
                                after: str = "", limit: int = 50):
        return vault.candidates.list_candidates(collection, status=status, after=after, limit=limit)

    @app.get("/collections/{collection}/corrections")
    def list_corrections_scoped(collection: str, status: str | None = None,
                                after: str = "", limit: int = 50):
        return vault.candidates.list_candidates(collection, status=status, after=after, limit=limit)

    @app.get("/candidates")
    def list_candidates(status: str | None = None, collection: str | None = None,
                        after: str = "", limit: int = 50):
        return vault.candidates.list_candidates(collection, status=status, after=after, limit=limit)

    @app.get("/candidates/{candidate}")
    def get_candidate(candidate: str):
        return vault.candidates.get(candidate)

    @app.get("/collections/{collection}/candidates/{candidate}")
    def get_candidate_scoped(collection: str, candidate: str):
        result = vault.candidates.get(candidate)
        if result["collection"] != collection:
            from strata.runtime.errors import missing
            raise missing("candidate", candidate)
        return result

    @app.delete("/candidates/{candidate}")
    def revoke_candidate(candidate: str):
        return vault.candidates.revoke(candidate)

    @app.delete("/collections/{collection}/candidates/{candidate}")
    def revoke_candidate_scoped(collection: str, candidate: str):
        result = vault.candidates.revoke(candidate)
        if result["collection"] != collection:
            from strata.runtime.errors import missing
            raise missing("candidate", candidate)
        return result

    @app.get("/collections/{collection}/candidates/{candidate}/plan")
    def scoped_candidate_plan(collection: str, candidate: str):
        result = vault.merges.plan(candidate)
        if result["collection"] != collection:
            from strata.runtime.errors import missing
            raise missing("candidate", candidate)
        return result

    @app.get("/collections/{collection}/candidates/{candidate}/conflicts")
    def scoped_candidate_conflicts(collection: str, candidate: str):
        return scoped_candidate_plan(collection, candidate)

    @app.post("/collections/{collection}/candidates/{candidate}/decisions")
    async def scoped_candidate_decide(collection: str, candidate: str, request: Request):
        body = await _json_object(request)
        decisions = body.get("decisions", body.get("resolutions", body.get("items")))
        if decisions is None and ("choice" in body or "key" in body or "conflict" in body):
            decisions = [body]
        return vault.merges.decide(candidate, decisions=decisions)

    @app.post("/collections/{collection}/candidates/{candidate}/publish-merge")
    async def scoped_candidate_publish(collection: str, candidate: str, request: Request):
        body = await _json_object(request, optional=True) or {}
        return vault.merges.publish(
            candidate, collection=collection,
            decisions=body.get("decisions", body.get("resolutions")),
            expected_head=body.get("expected_head", body.get("head")))

    @app.post("/collections/{collection}/candidates/{candidate}/merge")
    async def scoped_candidate_merge(collection: str, candidate: str, request: Request):
        return await scoped_candidate_publish(collection, candidate, request)

    @app.put("/collections/{collection}/candidates/{candidate}/parts/{digest}")
    async def scoped_candidate_part(candidate: str, digest: str, request: Request):
        size = await run_in_threadpool(vault.candidates.required_size, candidate, digest)
        with await uploaded(request, size, vault.delivery.scratch) as source:
            return await run_in_threadpool(vault.candidates.receive, candidate, digest, source)

    @app.get("/collections/{collection}/candidates/{candidate}/file")
    def scoped_candidate_file(candidate: str, request: Request,
                              path: Annotated[str, Query()]):
        checkout = vault.delivery.candidate_file(
            candidate, path, request.headers.get("range"))
        return download(checkout, media_type="application/octet-stream")

    @app.post("/candidates/{candidate}/conflicts/{conflict}/resolve")
    async def resolve_named_conflict(candidate: str, conflict: str, request: Request):
        body = await _json_object(request, optional=True) or {}
        decision = dict(body)
        decision.setdefault("key", conflict)
        return vault.merges.decide(candidate, decisions=[decision])

    for action, method_name in (("revoke", "revoke"), ("cancel", "cancel"),
                                ("withdraw", "withdraw")):
        def _make_revoke(name):
            def handler(candidate: str):
                return getattr(vault.candidates, name)(candidate)
            return handler
        app.add_api_route(f"/candidates/{{candidate}}/{action}", _make_revoke(method_name),
                         methods=["POST"])

    for action, method_name in (("close", "close"), ("finish", "finish"),
                                ("conclude", "conclude")):
        def _make_close(name):
            def handler(candidate: str):
                return getattr(vault.candidates, name)(candidate)
            return handler
        app.add_api_route(f"/candidates/{{candidate}}/{action}", _make_close(method_name),
                         methods=["POST"])

    @app.put("/candidates/{candidate}/parts/{digest}")
    async def receive_candidate_part(candidate: str, digest: str, request: Request):
        size = await run_in_threadpool(vault.candidates.required_size, candidate, digest)
        with await uploaded(request, size, vault.delivery.scratch) as source:
            return await run_in_threadpool(vault.candidates.receive, candidate, digest, source)

    @app.get("/candidates/{candidate}/file")
    def candidate_file(candidate: str, request: Request,
                       path: Annotated[str, Query()]):
        checkout = vault.delivery.candidate_file(
            candidate, path, request.headers.get("range"))
        return download(checkout, media_type="application/octet-stream")

    # ------------------------------------------------------------------
    # 三方合订：计划 / 决定 / 复核 / 发布 / 查询
    # ------------------------------------------------------------------
    @app.get("/candidates/{candidate}/plan")
    def candidate_plan(candidate: str):
        return vault.merges.plan(candidate)

    @app.post("/candidates/{candidate}/plan")
    async def candidate_plan_post(candidate: str, request: Request):
        body = await _json_object(request, optional=True)
        return vault.merges.plan(candidate, **(body or {}))

    @app.get("/candidates/{candidate}/conflicts")
    def candidate_conflicts(candidate: str):
        return vault.merges.plan(candidate)

    @app.post("/candidates/{candidate}/decisions")
    async def candidate_decide(candidate: str, request: Request):
        body = await _json_object(request)
        decisions = body.get("decisions", body.get("resolutions", body.get("items")))
        if decisions is None and ("choice" in body or "key" in body or "conflict" in body):
            decisions = [body]
        return vault.merges.decide(candidate, decisions=decisions)

    @app.post("/candidates/{candidate}/review")
    async def candidate_review(candidate: str, request: Request):
        body = await _json_object(request, optional=True)
        decisions = None if not body else body.get("decisions", body.get("resolutions"))
        if decisions is None and body and ("choice" in body or "key" in body):
            decisions = [body]
        if decisions is None:
            return vault.merges.plan(candidate)
        return vault.merges.decide(candidate, decisions=decisions)

    @app.post("/candidates/{candidate}/resolve")
    async def candidate_resolve(candidate: str, request: Request):
        body = await _json_object(request, optional=True)
        body = body or {}
        decisions = body.get("decisions", body.get("resolutions", body.get("items")))
        if decisions is None:
            # 整个 body 即一条决定（可含 key/conflict/id/type/path/choice）。
            decisions = [body] if body else None
        return vault.merges.decide(candidate, decisions=decisions)

    @app.post("/candidates/{candidate}/approve")
    async def candidate_approve(candidate: str, request: Request):
        return await candidate_resolve(candidate, request)

    @app.post("/candidates/{candidate}/reconfirm")
    def candidate_reconfirm(candidate: str):
        return vault.merges.reconfirm(candidate)

    @app.post("/candidates/{candidate}/publish-merge")
    async def candidate_publish_merge(candidate: str, request: Request):
        body = await _json_object(request, optional=True)
        body = body or {}
        return vault.merges.publish(
            candidate, decisions=body.get("decisions", body.get("resolutions")),
            expected_head=body.get("expected_head", body.get("head")))

    @app.post("/candidates/{candidate}/merge")
    async def candidate_merge_alias(candidate: str, request: Request):
        return await candidate_publish_merge(candidate, request)

    @app.post("/candidates/{candidate}/publish")
    async def candidate_publish_alias(candidate: str, request: Request):
        return await candidate_publish_merge(candidate, request)

    @app.get("/candidates/{candidate}/merge")
    def candidate_get_merge(candidate: str):
        return vault.merges.get_merge(candidate)

    @app.post("/collections/{collection}/merges/plan")
    async def collection_merge_plan(collection: str, request: Request):
        body = await _json_object(request)
        candidate = body.get("candidate", body.get("candidate_id", body.get("id")))
        return vault.merges.plan(candidate, collection=collection)

    @app.post("/collections/{collection}/merges/publish")
    async def collection_merge_publish(collection: str, request: Request):
        body = await _json_object(request)
        candidate = body.get("candidate", body.get("candidate_id", body.get("id")))
        return vault.merges.publish(
            candidate, collection=collection,
            decisions=body.get("decisions", body.get("resolutions")),
            expected_head=body.get("expected_head", body.get("head")))

    @app.get("/collections/{collection}/merges")
    def list_merges(collection: str, after: int = 0, limit: int = 50):
        return vault.merges.list_merges(collection, after=after, limit=limit)

    @app.get("/review-queue")
    def review_queue_all(limit: int = 50):
        return vault.merges.queue(None, limit=limit)

    @app.get("/collections/{collection}/review-queue")
    def review_queue_scoped(collection: str, limit: int = 50):
        return vault.merges.queue(collection, limit=limit)

    @app.get("/merges/queue")
    def merges_queue(limit: int = 50):
        return vault.merges.queue(None, limit=limit)

    # /corrections/... 作为 /candidates/... 的近义路径。
    for alias_path, endpoint, methods in (
        ("/corrections/{candidate}", get_candidate, ["GET"]),
        ("/corrections/{candidate}", revoke_candidate, ["DELETE"]),
        ("/corrections/{candidate}/plan", candidate_plan, ["GET"]),
        ("/corrections/{candidate}/plan", candidate_plan_post, ["POST"]),
        ("/corrections/{candidate}/conflicts", candidate_conflicts, ["GET"]),
        ("/corrections/{candidate}/decisions", candidate_decide, ["POST"]),
        ("/corrections/{candidate}/resolve", candidate_resolve, ["POST"]),
        ("/corrections/{candidate}/approve", candidate_approve, ["POST"]),
        ("/corrections/{candidate}/review", candidate_review, ["POST"]),
        ("/corrections/{candidate}/reconfirm", candidate_reconfirm, ["POST"]),
        ("/corrections/{candidate}/publish-merge", candidate_publish_merge, ["POST"]),
        ("/corrections/{candidate}/merge", candidate_merge_alias, ["POST"]),
        ("/corrections/{candidate}/merge", candidate_get_merge, ["GET"]),
        ("/corrections/{candidate}/file", candidate_file, ["GET"]),
        ("/corrections/{candidate}/parts/{digest}", receive_candidate_part, ["PUT"]),
    ):
        app.add_api_route(alias_path, endpoint, methods=methods)

    @app.get("/collections/{collection}/revisions")
    def list_revisions(collection: str, after: int = 0, limit: int = 50):
        return vault.catalog.revisions(collection, after, limit)

    @app.get("/collections/{collection}/revisions/{number}")
    def get_revision(collection: str, number: int):
        return vault.catalog.revision(collection, number)

    @app.put("/collections/{collection}/revisions/{number}/holds/{reference}")
    def place_hold(collection: str, number: int, reference: str, body: HoldInput):
        return vault.custody.hold(collection, number, reference, body.reason)

    @app.delete("/collections/{collection}/revisions/{number}/holds/{reference}")
    def release_hold(collection: str, number: int, reference: str):
        return vault.custody.release(collection, number, reference)

    @app.post("/collections/{collection}/revisions/{number}/retire")
    def retire_revision(collection: str, number: int):
        row = vault.custody.retire(collection, number)
        return {"collection": collection, "revision": number, "retired_at": row["retired_at"]}

    @app.get("/collections/{collection}/revisions/{number}/file")
    def checkout_file(collection: str, number: int, request: Request,
                      path: Annotated[str, Query()]):
        revision = vault.delivery.file(collection, number, path, request.headers.get("range"))
        return download(revision, media_type="application/octet-stream")

    @app.get("/collections/{collection}/revisions/{number}/package")
    def checkout_package(collection: str, number: int):
        return download(vault.delivery.package(collection, number), media_type="application/zip")

    @app.get("/operations/forecast")
    def capacity(limit: int = 50):
        return vault.custody.forecast(limit)

    @app.post("/operations/expire")
    def expire(limit: int = 100):
        expired_deposits = vault.deposits.expire(limit)
        expired_candidates = vault.candidates.expire(limit)
        return {"expired": [*expired_deposits, *expired_candidates],
                "deposits": expired_deposits,
                "candidates": expired_candidates,
                "expired_deposits": expired_deposits,
                "expired_candidates": expired_candidates}

    @app.post("/operations/expire-candidates")
    def expire_candidates(limit: int = 100):
        return {"candidates": vault.candidates.expire(limit)}

    @app.post("/operations/mark")
    def mark(limit: int = 100):
        return {"marked": vault.custody.mark(limit)}

    @app.post("/operations/sweep")
    def sweep(limit: int = 100):
        return vault.custody.sweep(limit)

    @app.post("/operations/reconcile")
    def reconcile(after: str = "", limit: int = 100, remove: bool = False):
        return vault.custody.reconcile(after, limit, remove=remove)

    @app.post("/operations/staging-cleanup")
    def clean_staging(limit: int = 100):
        return {"removed": vault.custody.clean_staging(limit)}

    @app.post("/operations/scan")
    def scan(after: str = "", limit: int = 25):
        return vault.integrity.scan(after, limit)

    @app.get("/operations/incidents")
    def incidents(after: int = 0, limit: int = 50, open_only: bool = True):
        return vault.integrity.incidents(after=after, limit=limit, open_only=open_only)

    @app.put("/operations/objects/{digest}/repair/{incident_id}")
    async def repair(digest: str, incident_id: int, request: Request):
        size = await run_in_threadpool(vault.integrity.repair_size, digest, incident_id)
        with await uploaded(request, size, vault.delivery.scratch) as source:
            return await run_in_threadpool(vault.integrity.repair, digest, incident_id, source)

    @app.get("/events")
    def events(collection: str | None = None, after: int = 0, limit: int = 50):
        return vault.events(collection, after, limit)

    @app.get("/collections/{collection}/revisions/{number}/coverage")
    def coverage(collection: str, number: int):
        return vault.inspection.coverage(collection, number)

    @app.get("/collections/{collection}/compare")
    def compare(collection: str, older: int, newer: int):
        return vault.inspection.compare(collection, older, newer)

    @app.get("/operations/objects/{digest}/references")
    def references(digest: str, limit: int = 50):
        return vault.inspection.object_references(digest, limit)

    @app.get("/operations/audit")
    def audit(collection: str | None = None, limit: int = 50):
        return vault.audit.check(collection, limit)

    @app.get("/collections/{collection}/footprint")
    def footprint(collection: str):
        return vault.footprint.collection(collection)

    return app


async def _json_object(request: Request, *, optional: bool = False) -> dict:
    from strata.runtime.jsonwire import read_json
    body = await request.body()
    if not body and optional:
        return {}
    try:
        value = read_json(body)
    except (ValueError, RecursionError):
        raise invalid("Request body must be unambiguous JSON") from None
    if not isinstance(value, dict):
        if optional and not body:
            return {}
        raise invalid("Request body must be a JSON object")
    return value
