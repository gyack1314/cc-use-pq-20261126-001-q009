"""将离线现场目录封装成支持续传的独立档案包。"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from tempfile import mkdtemp
import shutil
import stat
from typing import BinaryIO

from strata.runtime.errors import invalid
from strata.runtime.jsonwire import read_json
from strata.runtime.media import Media, sync_directory
from strata.transfer.manifest import (
    MAX_ARCHIVE, MAX_FILES, MAX_PART, MAX_PARTS, File, Manifest, Part,
    archive_path, collection_id, integer, parse_manifest, reference_id,
)

FORMAT = "strata-field-parcel-v1"
DEFAULT_PART = 4 * 1024 * 1024
DESCRIPTOR = "parcel.json"
MAX_DESCRIPTOR = 2 * 1024 * 1024


@dataclass(frozen=True)
class Parcel:
    root: Path
    collection: str
    token: str
    parent: int
    lifetime_seconds: int
    manifest: Manifest

    @property
    def collection_body(self) -> dict:
        return {
            "token": self.token,
            "parent": self.parent,
            "lifetime_seconds": self.lifetime_seconds,
            "manifest": self.manifest.as_dict(),
        }

    def descriptor(self) -> dict:
        return {
            "format": FORMAT,
            "collection": self.collection,
            "manifest_sha256": self.manifest.fingerprint(),
            **self.collection_body,
        }

    def media(self) -> Media:
        root = self.root / "parts"
        if not root.is_dir() or root.is_symlink():
            raise invalid("Parcel is missing the portable parts directory")
        return Media(root)


def open_parcel(root: Path) -> Parcel:
    descriptor = root / DESCRIPTOR
    try:
        if descriptor.is_symlink():
            raise invalid("Parcel descriptor must be a regular file")
        with descriptor.open("rb") as source:
            raw = source.read(MAX_DESCRIPTOR + 1)
    except OSError as error:
        raise invalid("Cannot open the parcel descriptor", detail=str(error)) from error
    if len(raw) > MAX_DESCRIPTOR:
        raise invalid("Parcel descriptor exceeds the byte limit")
    try:
        record = read_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise invalid("Parcel descriptor is not unambiguous JSON") from None
    expected = {"format", "collection", "token", "parent", "lifetime_seconds", "manifest", "manifest_sha256"}
    if not isinstance(record, dict) or set(record) != expected or record["format"] != FORMAT:
        raise invalid("Parcel descriptor has an unknown format")
    collection, token = record["collection"], record["token"]
    if not isinstance(collection, str) or not isinstance(token, str):
        raise invalid("Parcel collection and token must be strings")
    parcel = Parcel(root, collection_id(collection), reference_id(token, "source token"),
                    integer(record["parent"], 0, 2**63 - 1, "parent"),
                    integer(record["lifetime_seconds"], 60, 30 * 86400, "deposit lifetime"),
                    parse_manifest(record["manifest"]))
    if record["manifest_sha256"] != parcel.manifest.fingerprint():
        raise invalid("Parcel manifest does not match its recorded digest")
    return parcel


def source_files(source: Path) -> list[Path]:
    result = []

    def scan_error(error):
        raise invalid("Could not read source directory", detail=str(error))

    for root, dirs, files in os.walk(source, followlinks=False, onerror=scan_error):
        base = Path(root)
        for directory in dirs:
            path = base / directory
            if path.is_symlink() or not path.is_dir():
                raise invalid("Source contains a linked or non-directory entry", path=str(path))
        for name in files:
            path = base / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise invalid("Parcel source must contain regular files", path=str(path))
            result.append(path)
            if len(result) > MAX_FILES:
                raise invalid("Too many source files", maximum=MAX_FILES)
    if not result:
        raise invalid("Source directory has no files")
    return sorted(result, key=lambda path: path.relative_to(source).as_posix())


def stat_identity(record: os.stat_result) -> tuple:
    return (record.st_dev, record.st_ino, record.st_size, record.st_mtime_ns, record.st_ctime_ns)


def build_parcel(source: Path, destination: Path, *, collection: str, token: str,
                 parent: int, lifetime: int, part_size: int = DEFAULT_PART) -> Parcel:
    collection_id(collection)
    reference_id(token, "source token")
    integer(parent, 0, 2**63 - 1, "parent")
    integer(lifetime, 60, 30 * 86400, "deposit lifetime")
    integer(part_size, 1, MAX_PART, "part size")
    if not source.is_dir() or source.is_symlink():
        raise invalid("Source must be a local directory")
    source = source.resolve()
    if os.path.lexists(destination):
        raise invalid("Parcel destination already exists", path=str(destination))
    destination = destination.resolve()
    if destination.is_relative_to(source):
        raise invalid("Parcel destination must be outside the source directory")
    inputs = source_files(source)
    if not destination.parent.is_dir():
        raise invalid("Parcel destination parent does not exist")
    scratch = Path(mkdtemp(prefix=".parcel-", dir=destination.parent))
    try:
        media = Media(scratch / "parts")
        files = []
        total = references = 0
        stored = set()
        for incoming in inputs:
            path = archive_path(incoming.relative_to(source).as_posix())
            previous = incoming.lstat()
            if not stat.S_ISREG(previous.st_mode):
                raise invalid("Source changed during snapshot", path=path)
            if total + previous.st_size > MAX_ARCHIVE:
                raise invalid("Source exceeds archive byte limit")
            with incoming.open("rb") as stream:
                if stat_identity(os.fstat(stream.fileno())) != stat_identity(previous):
                    raise invalid("Source changed before capture", path=path)
                entry = _capture(path, stream, previous.st_size, part_size, media, stored, MAX_PARTS - references)
                if stat_identity(os.fstat(stream.fileno())) != stat_identity(previous):
                    raise invalid("Source changed during capture", path=path)
            if stat_identity(incoming.lstat()) != stat_identity(previous):
                raise invalid("Source was replaced during capture", path=path)
            files.append(entry)
            total += entry.size
            references += len(entry.parts)
        manifest = parse_manifest(Manifest(tuple(files)).as_dict())
        parcel = Parcel(destination, collection, token, parent, lifetime, manifest)
        _seal_descriptor(scratch / DESCRIPTOR, parcel)
        sync_directory(scratch)
        if os.path.lexists(destination):
            raise invalid("Parcel destination already exists", path=str(destination))
        scratch.rename(destination)
        sync_directory(destination.parent)
        return parcel
    finally:
        if scratch.is_dir():
            shutil.rmtree(scratch)


def _capture(path: str, stream: BinaryIO, size: int, part_size: int, media: Media,
             stored: set[str], reference_budget: int) -> File:
    from io import BytesIO

    remaining = size
    digest = hashlib.sha256()
    parts = []
    while remaining:
        length = min(part_size, remaining)
        block = stream.read(length)
        if len(block) != length:
            raise invalid("File shrank during capture", path=path)
        if len(parts) >= reference_budget:
            raise invalid("Source exceeds the archive part count")
        part_digest = hashlib.sha256(block).hexdigest()
        if part_digest not in stored:
            media.install(part_digest, length, BytesIO(block))
            stored.add(part_digest)
        digest.update(block)
        parts.append(Part(part_digest, length))
        remaining -= length
    if stream.read(1):
        raise invalid("File grew during capture", path=path)
    return File(path, size, digest.hexdigest(), tuple(parts))


def _seal_descriptor(path: Path, parcel: Parcel) -> None:
    encoded = json.dumps(parcel.descriptor(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) >= MAX_DESCRIPTOR:
        raise invalid("Generated descriptor exceeds byte limit")
    with path.open("xb") as target:
        target.write(encoded)
        target.write(b"\n")
        target.flush()
        os.fsync(target.fileno())
