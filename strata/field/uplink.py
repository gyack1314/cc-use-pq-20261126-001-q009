"""按接收方缺片回执提交现场档案包。"""

from dataclasses import dataclass
import re
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from strata.field.parcel import Parcel
from strata.runtime.errors import VaultError, invalid, unavailable
from strata.runtime.media import BLOCK_SIZE



class Endpoint(Protocol):
    def request(self, method: str, path: str, **kwargs) -> dict:
        ...


class Client:
    def __init__(self, base_url: str, timeout: float = 30):
        location = urlsplit(base_url)
        if (location.scheme not in ("http", "https") or not location.hostname
                or location.username or location.password or location.query or location.fragment
                or location.path not in ("", "/")):
            raise invalid("Vault URL must be an HTTP origin")
        self.client = httpx.Client(base_url=base_url, timeout=timeout, trust_env=False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def request(self, method: str, path: str, **kwargs) -> dict:
        try:
            reply = self.client.request(method, path, **kwargs)
        except httpx.RequestError as error:
            raise unavailable("Could not confirm vault operation; retry with the same parcel",
                              method=method, path=path) from error
        try:
            value = reply.json()
        except ValueError as error:
            raise unavailable("Vault did not return a JSON receipt", status=reply.status_code) from error
        if not isinstance(value, dict):
            raise unavailable("Vault response was not an object", status=reply.status_code)
        if not reply.is_success:
            detail = value.get("error", {})
            if not isinstance(detail, dict):
                detail = {}
            raise VaultError(str(detail.get("code", "remote_request_failed")),
                             str(detail.get("message", "Vault rejected the request")),
                             reply.status_code, remote=detail.get("details", {}))
        return value


@dataclass(frozen=True)
class Accepted:
    deposit: str
    status: str
    revision: int | None
    transmitted: int

    def as_dict(self) -> dict:
        return {"deposit": self.deposit, "status": self.status, "revision": self.revision,
                "transmitted": self.transmitted}


class Uplink:
    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint

    def send(self, parcel: Parcel, *, publish: bool = True, maximum_parts: int | None = None) -> Accepted:
        expected = parcel.manifest.unique_parts()
        if maximum_parts is not None and (type(maximum_parts) is not int or maximum_parts < 0):
            raise invalid("Part limit must be a non-negative integer")
        reply = self.endpoint.request("POST", f"/collections/{parcel.collection}/deposits",
                                      json=parcel.collection_body)
        session, pending = self._accept_open(parcel, reply, expected)
        if reply["status"] == "published":
            return Accepted(session, "published", self._revision(reply), 0)
        if reply["status"] != "open":
            raise VaultError("deposit_closed", "Vault session no longer accepts this parcel", 409,
                             deposit=session, status=reply["status"])
        remaining = pending if maximum_parts is None else pending[:maximum_parts]
        media = parcel.media()
        sent = 0
        for digest in remaining:
            size = expected[digest]
            media.verify(digest, size)
            with media.path(digest).open("rb") as stream:
                result = self.endpoint.request(
                    "PUT", f"/deposits/{session}/parts/{digest}",
                    content=iter(lambda: stream.read(BLOCK_SIZE), b""),
                    headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
                )
            if (result.get("deposit"), result.get("digest"), result.get("size")) != (session, digest, size):
                raise unavailable("Receipt does not identify the transmitted part", digest=digest)
            sent += 1
        if not publish or len(remaining) != len(pending):
            return Accepted(session, "open", None, sent)
        result = self.endpoint.request("POST", f"/deposits/{session}/publish")
        if (result.get("deposit") != session or result.get("collection") != parcel.collection
                or result.get("manifest_sha256") != parcel.manifest.fingerprint()
                or result.get("parent") != parcel.parent):
            raise unavailable("Publish receipt does not match the parcel", deposit=session)
        return Accepted(session, "published", self._revision(result), sent)

    def _accept_open(self, parcel: Parcel, value: dict, expected: dict[str, int]) -> tuple[str, list[str]]:
        session = value.get("id")
        if not isinstance(session, str) or not re.fullmatch(r"[0-9a-f]{32}", session):
            raise unavailable("Vault did not return a valid deposit identity")
        if (value.get("collection") != parcel.collection or value.get("token") != parcel.token
                or value.get("manifest_sha256") != parcel.manifest.fingerprint()
                or value.get("parent") != parcel.parent):
            raise unavailable("Vault deposit does not match the requested parcel", deposit=session)
        pending = value.get("pending")
        status = value.get("status")
        if not isinstance(pending, list) or status not in ("open", "published", "expired", "canceled"):
            raise unavailable("Vault response has an invalid session state", deposit=session)
        digests = []
        seen = set()
        for item in pending:
            if not isinstance(item, dict):
                raise unavailable("Vault returned an invalid pending part")
            digest = item.get("digest")
            size = item.get("size")
            if not isinstance(digest, str) or type(size) is not int or expected.get(digest) != size or digest in seen:
                raise unavailable("Vault returned an unexpected pending part")
            seen.add(digest)
            digests.append(digest)
        if status != "open" and digests:
            raise unavailable("Closed deposit cannot list pending parts")
        return session, digests

    @staticmethod
    def _revision(reply: dict) -> int:
        number = reply.get("revision")
        if type(number) is not int or number < 1:
            raise unavailable("Vault did not confirm a revision number")
        return number
