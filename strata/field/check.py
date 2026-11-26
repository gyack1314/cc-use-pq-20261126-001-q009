"""在交接运输前校验现场缓存中的档案分片。"""

from dataclasses import dataclass


from strata.delivery.assembly import file_blocks
from strata.field.parcel import Parcel
from strata.runtime.errors import VaultError
from strata.transfer.manifest import integer


@dataclass(frozen=True)
class Check:
    files: int
    unique_parts: int
    logical_bytes: int
    available_bytes: int
    findings: tuple[dict, ...]
    errors: int

    def as_dict(self) -> dict:
        return {
            "files": self.files, "unique_parts": self.unique_parts,
            "logical_bytes": self.logical_bytes, "available_bytes": self.available_bytes,
            "findings": list(self.findings), "errors": self.errors, "valid": self.errors == 0,
        }


def check_parcel(parcel: Parcel, limit: int = 50) -> Check:
    integer(limit, 1, 200, "parcel finding limit")
    media = parcel.media()
    expected = parcel.manifest.unique_parts()
    corrupt = set()
    findings = []
    errors = 0
    available = 0
    for digest, size in sorted(expected.items()):
        try:
            media.verify(digest, size)
        except VaultError as error:
            if error.code != "object_unreadable":
                raise
            corrupt.add(digest)
            errors += 1
            if len(findings) < limit:
                findings.append({"digest": digest, "condition": error.details["reason"]})
        else:
            available += size
    files = parcel.manifest.files
    for entry in files:
        if any(part.digest in corrupt for part in entry.parts):
            continue
        try:
            for _ in file_blocks(media, entry):
                pass
        except VaultError as error:
            if error.code != "file_digest_mismatch":
                raise
            errors += 1
            if len(findings) < limit:
                findings.append({"path": entry.path, "condition": "file_digest_mismatch"})
    return Check(len(files), len(expected), sum(entry.size for entry in files),
                 available, tuple(findings), errors)
