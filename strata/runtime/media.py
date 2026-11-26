"""用本地文件保存内容寻址对象。调用方通过目录写事务协调发布、修复和删除。已存在对象仅允许通过完整校验的原子替换修复。"""

from collections.abc import Iterator
import errno
import hashlib
from pathlib import Path
import os
import re
from typing import BinaryIO
from uuid import uuid4

from strata.runtime.errors import damaged, invalid, unavailable
from strata.transfer.manifest import sha256

BLOCK_SIZE = 256 * 1024
SHARD = re.compile(r"[0-9a-f]{2}\Z")


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Media:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, digest: str) -> Path:
        digest = sha256(digest)
        return self.root / digest[:2] / digest[2:]

    def measure(self, source: BinaryIO, size: int, output: BinaryIO | None = None) -> str:
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = source.read(min(BLOCK_SIZE, remaining))
            if not chunk:
                raise EOFError
            if output is not None:
                output.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if source.read(1):
            raise OverflowError
        return digest.hexdigest()

    def validate_upload(self, digest: str, size: int, source: BinaryIO) -> None:
        try:
            source.seek(0)
            actual = self.measure(source, size)
        except (EOFError, OverflowError):
            raise invalid("Uploaded bytes do not match expected length", expected=size) from None
        if actual != digest:
            raise invalid("Uploaded bytes do not match expected digest", digest=digest)

    def install(self, digest: str, size: int, source: BinaryIO) -> None:
        path = self.path(digest)
        temp = path.with_name(f".pending-{uuid4().hex}")
        created_directory = False
        try:
            try:
                path.parent.mkdir()
                created_directory = True
            except FileExistsError:
                pass
            if created_directory:
                sync_directory(self.root)
            with temp.open("xb") as target:
                source.seek(0)
                actual = self.measure(source, size, target)
                if actual != digest:
                    raise invalid("Replacement bytes do not match expected digest", digest=digest)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, path)
            sync_directory(path.parent)
        except (EOFError, OverflowError):
            raise invalid("Replacement bytes do not match expected length", expected=size) from None
        except OSError as error:
            raise unavailable("Failed to store archive object", digest=digest) from error
        finally:
            temp.unlink(missing_ok=True)

    def blocks(self, digest: str, size: int) -> Iterator[bytes]:
        path = self.path(digest)
        computed = hashlib.sha256()
        remaining = size
        try:
            with path.open("rb") as source:
                while remaining:
                    chunk = source.read(min(remaining, BLOCK_SIZE))
                    if not chunk:
                        raise damaged(digest, "length_mismatch")
                    computed.update(chunk)
                    remaining -= len(chunk)
                    yield chunk
                if source.read(1):
                    raise damaged(digest, "length_mismatch")
        except FileNotFoundError:
            raise damaged(digest, "missing") from None
        except OSError as error:
            if error.errno in (errno.EIO, errno.EISDIR):
                raise damaged(digest, "unreadable") from error
            raise unavailable("Could not read archive media", digest=digest) from error
        if computed.hexdigest() != digest:
            raise damaged(digest, "digest_mismatch")

    def verify(self, digest: str, size: int) -> None:
        for _ in self.blocks(digest, size):
            pass

    def remove(self, digest: str) -> None:
        path = self.path(digest)
        try:
            path.unlink(missing_ok=True)
            if path.parent.is_dir():
                sync_directory(path.parent)
        except OSError as error:
            raise unavailable("Could not reclaim archive object", digest=digest) from error

    def inventory(self, after: str = "") -> Iterator[str]:
        if after:
            sha256(after)
        for shard in sorted(self.root.iterdir()):
            if not SHARD.fullmatch(shard.name) or shard.name < after[:2] or not shard.is_dir():
                continue
            for entry in sorted(shard.iterdir()):
                digest = shard.name + entry.name
                if len(digest) == 64 and digest > after and not re.search(r"[^0-9a-f]", digest):
                    if entry.is_file():
                        yield digest
