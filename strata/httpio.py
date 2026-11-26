"""限制请求的字节数，并流式发送已完成校验的响应。"""

from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from fastapi import Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from strata.delivery.service import Checkout, SPOOL_LIMIT
from strata.runtime.errors import VaultError, invalid
from strata.runtime.jsonwire import read_json
from strata.runtime.media import BLOCK_SIZE

MAX_JSON = 2 * 1024 * 1024


def error_response(error: VaultError) -> JSONResponse:
    headers = {"Content-Range": f"bytes */{error.details['size']}"} if error.status == 416 else None
    return JSONResponse(status_code=error.status, content={"error": error.as_dict()}, headers=headers)


class JsonLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].lower().strip()
        if content_type == b"application/octet-stream":
            await self.app(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            event = await receive()
            if event["type"] != "http.request":
                return
            chunk = event.get("body", b"")
            size += len(chunk)
            if size > MAX_JSON:
                error = VaultError("request_too_large", "Request exceeds JSON byte limit", 413, limit=MAX_JSON)
                await error_response(error)(scope, receive, send)
                return
            chunks.append(chunk)
            if not event.get("more_body", False):
                break

        body = b"".join(chunks)
        if body and (content_type == b"application/json" or content_type.endswith(b"+json")):
            try:
                read_json(body)
            except (ValueError, RecursionError):
                error = invalid("Request JSON must be unambiguous UTF-8")
                await error_response(error)(scope, receive, send)
                return

        issued = False

        async def replay():
            nonlocal issued
            if issued:
                return await receive()
            issued = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


async def uploaded(request: Request, expected: int, scratch):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower().strip()
    if content_type != "application/octet-stream":
        raise VaultError("unsupported_media_type", "Use application/octet-stream for archive bytes", 415)
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isascii() or not declared.isdecimal()
                                 or len(declared) > 19 or int(declared) != expected):
        raise invalid("Content length must match the expected object size", expected=expected)
    size = 0
    spool = SpooledTemporaryFile(max_size=SPOOL_LIMIT, dir=scratch)
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > expected:
                raise invalid("Upload exceeds the expected length", expected=expected)
            spool.write(chunk)
        if size != expected:
            raise invalid("Upload ended before the expected length", expected=expected, actual=size)
        spool.seek(0)
        return spool
    except BaseException:
        spool.close()
        raise


def download(checkout: Checkout, *, media_type: str) -> StreamingResponse:
    partial = checkout.span is not None and checkout.span.partial
    headers = {
        "Content-Length": str(checkout.length),
        "X-Manifest-SHA256": checkout.manifest_hash,
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(checkout.name, safe=""),
    }
    if checkout.file_hash is not None:
        headers["ETag"] = '"' + checkout.file_hash + '"'
        headers["Accept-Ranges"] = "bytes"
    if partial:
        headers["Content-Range"] = checkout.span.content_range

    def blocks():
        try:
            while chunk := checkout.spool.read(BLOCK_SIZE):
                yield chunk
        finally:
            checkout.close()

    return StreamingResponse(blocks(), media_type=media_type, headers=headers,
                             status_code=206 if partial else 200, background=BackgroundTask(checkout.close))
