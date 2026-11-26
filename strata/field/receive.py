"""使用可信引用校验档案包，并支持验证后的解包。"""

from dataclasses import dataclass
import hashlib
from strata.runtime.jsonwire import read_json
import os
from pathlib import Path
import shutil
import stat
from tempfile import mkdtemp
from typing import BinaryIO
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from strata.field.parcel import MAX_DESCRIPTOR
from strata.runtime.errors import conflict, invalid
from strata.runtime.media import BLOCK_SIZE, sync_directory
from strata.transfer.manifest import MAX_FILES, collection_id, integer, parse_manifest, sha256

MANIFEST = "_vault/manifest.json"
RECEIPT = "_vault/receipt.json"


@dataclass(frozen=True)
class Recovery:
    collection: str
    revision: int
    parent: int
    manifest_sha256: str
    files: int
    bytes: int

    def as_dict(self) -> dict:
        return {
            "collection": self.collection, "revision": self.revision, "parent": self.parent,
            "manifest_sha256": self.manifest_sha256, "files": self.files, "bytes": self.bytes,
        }


def verify_package(source: Path, *, expected_manifest: str, expected_collection: str,
                   expected_revision: int, destination: Path | None = None) -> Recovery:
    sha256(expected_manifest)
    collection_id(expected_collection)
    integer(expected_revision, 1, 2**63 - 1, "revision")
    scratch = None
    if destination is not None:
        if os.path.lexists(destination):
            raise conflict("Recovery destination already exists")
        destination = destination.resolve()
        if not destination.parent.is_dir():
            raise invalid("Recovery destination parent does not exist")
        scratch = Path(mkdtemp(prefix=".recovery-", dir=destination.parent))
    try:
        try:
            with ZipFile(source, "r") as package:
                summary = _verify(package, expected_manifest, expected_collection, expected_revision, scratch)
        except (BadZipFile, EOFError, UnicodeError, NotImplementedError) as error:
            raise invalid("Recovery package has an invalid ZIP structure") from error
        if scratch is not None:
            _sync_tree(scratch)
            if os.path.lexists(destination):
                raise conflict("Recovery destination already exists")
            scratch.rename(destination)
            sync_directory(destination.parent)
        return summary
    finally:
        if scratch is not None and scratch.is_dir():
            shutil.rmtree(scratch)


def _verify(package: ZipFile, fingerprint: str, collection: str,
            revision: int, scratch: Path | None) -> Recovery:
    members = _members(package)
    receipt = _metadata(package, members, RECEIPT, 4096)
    fields = {"collection", "revision", "parent", "manifest_sha256", "published_at"}
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise invalid("Recovery receipt has unknown fields")
    number = integer(receipt["revision"], 1, 2**63 - 1, "receipt revision")
    parent = integer(receipt["parent"], 0, 2**63 - 1, "receipt parent")
    integer(receipt["published_at"], 0, 2**63 - 1, "receipt publication time")
    if (receipt["manifest_sha256"], receipt["collection"], number) != (fingerprint, collection, revision):
        raise conflict("Package identity differs from the requested archival reference")
    if parent != number - 1:
        raise invalid("Recovery receipt has an inconsistent parent")
    manifest = parse_manifest(_metadata(package, members, MANIFEST, MAX_DESCRIPTOR))
    if manifest.fingerprint() != fingerprint:
        raise conflict("Recovery manifest does not match the archival reference")
    advertised = {MANIFEST, RECEIPT, *(entry.path for entry in manifest.files)}
    if set(members) != advertised:
        raise invalid("ZIP membership does not match the archived file list",
                      missing=sorted(advertised - set(members)), extra=sorted(set(members) - advertised))
    total = 0
    for entry in manifest.files:
        info = members[entry.path]
        if info.file_size != entry.size:
            raise invalid("ZIP member size differs from the manifest", path=entry.path)
        with package.open(info) as stream:
            if scratch is None:
                _check_bytes(stream, entry)
            else:
                target = scratch.joinpath(*entry.path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    _check_bytes(stream, entry, output)
                    output.flush()
                    os.fsync(output.fileno())
        total += entry.size
    return Recovery(collection, revision, parent, fingerprint, len(manifest.files), total)


def _members(package: ZipFile) -> dict[str, ZipInfo]:
    entries = package.infolist()
    if not 3 <= len(entries) <= MAX_FILES + 2:
        raise invalid("Recovery package has an invalid member count")
    members = {}
    for info in entries:
        mode = info.external_attr >> 16
        if (info.orig_filename != info.filename or info.filename in members or info.is_dir()
                or (info.create_system == 3 and stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                or info.compress_type != ZIP_STORED or info.flag_bits & 1
                or info.compress_size != info.file_size):
            raise invalid("Recovery package contains an unsupported ZIP member", path=info.filename)
        members[info.filename] = info
    return members


def _metadata(package: ZipFile, members: dict[str, ZipInfo], path: str, limit: int):
    info = members.get(path)
    if info is None or not 1 <= info.file_size <= limit:
        raise invalid("Recovery package metadata is missing or exceeds its limit", path=path)
    with package.open(info) as stream:
        try:
            return read_json(stream.read(limit + 1))
        except (ValueError, RecursionError):
            raise invalid("Recovery metadata must be unambiguous JSON", path=path) from None


def _check_bytes(source: BinaryIO, entry, output: BinaryIO | None = None) -> None:
    file_digest = hashlib.sha256()
    for part in entry.parts:
        part_digest = hashlib.sha256()
        remaining = part.size
        while remaining:
            block = source.read(min(remaining, BLOCK_SIZE))
            if not block:
                raise invalid("Recovery file ended before its declared size", path=entry.path)
            remaining -= len(block)
            part_digest.update(block)
            file_digest.update(block)
            if output is not None:
                output.write(block)
        if part_digest.hexdigest() != part.digest:
            raise conflict("Recovery file part does not match the manifest", path=entry.path, digest=part.digest)
    if source.read(1) or file_digest.hexdigest() != entry.digest:
        raise conflict("Recovery file does not match the manifest", path=entry.path)


def _sync_tree(root: Path) -> None:
    for directory, _, _ in os.walk(root, topdown=False):
        sync_directory(Path(directory))
