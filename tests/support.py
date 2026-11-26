from contextlib import ExitStack
import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from strata.api import create_app
from strata.runtime.vault import Vault


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest(entries: dict[str, list[bytes]]) -> dict:
    files = []
    for path, parts in entries.items():
        data = b"".join(parts)
        files.append({"path": path, "size": len(data), "sha256": sha(data),
                      "parts": [{"sha256": sha(part), "size": len(part)} for part in parts]})
    return {"version": 1, "files": files}


class Clock:
    def __init__(self):
        self.value = 1_700_000_000

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += seconds


class Scenario(unittest.TestCase):
    def setUp(self):
        self.resources = self.enterContext(ExitStack())
        self.root = Path(self.resources.enter_context(TemporaryDirectory()))
        self.clock = Clock()
        self.app = create_app(self.root / "vault", clock=self.clock)
        self.http = self.resources.enter_context(TestClient(self.app))
        self.vault = self.app.state.vault
        self.vault.catalog.create_collection("ridge", 60)

    def open(self, token, entries, *, parent=0, lifetime=60, collection="ridge"):
        return self.vault.deposits.open(collection, token, parent, lifetime, manifest(entries))

    def upload(self, deposit, data):
        return self.vault.deposits.receive(deposit, sha(data), BytesIO(data))

    def edition(self, token, entries, *, parent=0, collection="ridge"):
        opened = self.open(token, entries, parent=parent, collection=collection)
        source = {sha(part): part for parts in entries.values() for part in parts}
        for part in opened["pending"]:
            self.upload(opened["id"], source[part["digest"]])
        return self.vault.catalog.publish(opened["id"])

    def restart(self):
        self.vault = Vault(self.root / "vault", self.clock)
