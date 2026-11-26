"""校验后交付归档版本的字节或便携完整档案包。"""

from dataclasses import dataclass
from pathlib import Path
import re
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from strata.delivery.assembly import file_blocks
from strata.preservation.catalog import damage, revision_row
from strata.preservation.integrity import report, verified
from strata.runtime.database import Database
from strata.runtime.errors import VaultError, conflict, missing
from strata.runtime.media import Media
from strata.transfer.manifest import archive_path, canonical, parse_stored

RANGE = re.compile(r"bytes=([0-9]{0,19})-([0-9]{0,19})\Z")
SPOOL_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    size: int
    partial: bool

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end - 1}/{self.size}"


def span_for(value: str | None, size: int) -> Span:
    if value is None:
        return Span(0, size, size, False)
    match = RANGE.fullmatch(value)
    if match is not None and size:
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last) + 1, size) if last else size
            if start < end:
                return Span(start, end, size, True)
        elif last and int(last):
            return Span(max(0, size - int(last)), size, size, True)
    raise VaultError("range_not_satisfiable", "Requested byte range is unavailable", 416, size=size)


@dataclass
class Checkout:
    spool: BinaryIO
    name: str
    length: int
    manifest_hash: str
    file_hash: str | None = None
    span: Span | None = None

    def close(self) -> None:
        self.spool.close()


class Delivery:
    def __init__(self, db: Database, media: Media, scratch: Path):
        self.db = db
        self.media = media
        self.scratch = scratch
        scratch.mkdir(parents=True, exist_ok=True)

    def _open_revision(self, connection, collection, number):
        row = revision_row(connection, collection, number)
        if row["retired_at"] is not None:
            raise VaultError("revision_retired", "Revision bytes have been retired", 410,
                             collection=collection, revision=number)
        return row, parse_stored(row["manifest"], row["manifest_hash"])

    def _check_incidents(self, connection, row, digests: set[str]) -> None:
        incidents = [item for item in damage(connection, row["collection"], row["number"])
                     if item["digest"] in digests]
        if incidents:
            raise conflict("Requested bytes depend on a damaged object", incidents=incidents)

    def file(self, collection: str, number: int, path: str, byte_range: str | None = None) -> Checkout:
        archive_path(path)
        spool = SpooledTemporaryFile(max_size=SPOOL_LIMIT, dir=self.scratch)
        failure = None
        try:
            with self.db.transaction() as connection:
                now = self.db.now()
                row, manifest = self._open_revision(connection, collection, number)
                entry = manifest.file(path)
                if entry is None:
                    raise missing("archive file", path)
                span = span_for(byte_range, entry.size)
                digests = {part.digest for part in entry.parts}
                self._check_incidents(connection, row, digests)
                try:
                    self._write_span(entry, span, spool)
                except VaultError as error:
                    report(connection, error, now)
                    failure = error
                else:
                    for digest in digests:
                        verified(connection, digest, now)
            if failure is not None:
                raise failure
            spool.seek(0)
            return Checkout(spool, path.rsplit("/", 1)[-1], span.length,
                            row["manifest_hash"], entry.digest, span)
        except BaseException:
            spool.close()
            raise

    def _write_span(self, entry, span: Span, spool: BinaryIO) -> None:
        offset = 0
        for block in file_blocks(self.media, entry):
            block_end = offset + len(block)
            first = max(span.start, offset)
            last = min(span.end, block_end)
            if first < last:
                spool.write(block[first - offset:last - offset])
            offset = block_end

    def package(self, collection: str, number: int) -> Checkout:
        spool = SpooledTemporaryFile(max_size=SPOOL_LIMIT, dir=self.scratch)
        failure = None
        try:
            with self.db.transaction() as connection:
                now = self.db.now()
                row, manifest = self._open_revision(connection, collection, number)
                digests = set(manifest.unique_parts())
                self._check_incidents(connection, row, digests)
                try:
                    self._write_package(spool, row, manifest)
                except VaultError as error:
                    report(connection, error, now)
                    failure = error
                else:
                    for digest in digests:
                        verified(connection, digest, now)
            if failure is not None:
                raise failure
            length = spool.tell()
            spool.seek(0)
            return Checkout(spool, f"{collection}-r{number}.zip", length, row["manifest_hash"])
        except BaseException:
            spool.close()
            raise

    def _write_package(self, spool, row, manifest) -> None:
        receipt = {
            "collection": row["collection"], "revision": row["number"], "parent": row["parent"],
            "manifest_sha256": row["manifest_hash"], "published_at": row["published_at"],
        }
        with ZipFile(spool, mode="w", compression=ZIP_STORED, allowZip64=True) as package:
            package.writestr(self._zip_entry("_vault/manifest.json"), row["manifest"].encode("utf-8"))
            package.writestr(self._zip_entry("_vault/receipt.json"), canonical(receipt).encode("utf-8"))
            for entry in manifest.files:
                info = self._zip_entry(entry.path)
                info.file_size = entry.size
                with package.open(info, "w", force_zip64=entry.size >= 2**31 - 1) as target:
                    for block in file_blocks(self.media, entry):
                        target.write(block)

    @staticmethod
    def _zip_entry(path: str) -> ZipInfo:
        info = ZipInfo(path)
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        return info
