"""规范化清单，并校验可跨系统使用的归档路径。"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
import unicodedata

from strata.runtime.errors import invalid

MAX_FILES = 500
MAX_PART = 16 * 1024 * 1024
MAX_PARTS = 8192
MAX_ARCHIVE = 4 * 1024 * 1024 * 1024
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
COLLECTION = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])\Z", re.IGNORECASE)
FORBIDDEN = frozenset('<>:"\\|?*')


def canonical(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(value: object) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise invalid("Expected a lowercase SHA-256 digest")
    return value


def collection_id(name: str) -> str:
    if not COLLECTION.fullmatch(name):
        raise invalid("Invalid collection name", collection=name)
    return name


def reference_id(value: str, label: str = "reference") -> str:
    if not TOKEN.fullmatch(value):
        raise invalid(f"Invalid {label}")
    return value


def integer(value: object, least: int, most: int, field: str) -> int:
    if type(value) is not int or not least <= value <= most:
        raise invalid(f"{field} is outside its supported range", minimum=least, maximum=most)
    return value


def archive_path(path: object) -> str:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise invalid("Invalid archive path length")
    if unicodedata.normalize("NFC", path) != path:
        raise invalid("Archive paths must be NFC", path=path)
    components = path.split("/")
    for component in components:
        if (not component or component in (".", "..") or component.startswith(" ")
                or component.endswith((" ", ".")) or len(component) > 100
                or DEVICE.fullmatch(component.split(".", 1)[0])
                or any(ch in FORBIDDEN or unicodedata.category(ch).startswith("C") for ch in component)):
            raise invalid("Archive path is not portable", path=path)
    if components[0].casefold() == "_vault":
        raise invalid("Archive path uses the reserved metadata namespace", path=path)
    return path


def check_fields(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise invalid(f"Unexpected fields for {label}", required=sorted(fields))
    return value


@dataclass(frozen=True)
class Part:
    digest: str
    size: int

    def as_dict(self) -> dict:
        return {"sha256": self.digest, "size": self.size}


@dataclass(frozen=True)
class File:
    path: str
    size: int
    digest: str
    parts: tuple[Part, ...]

    def as_dict(self) -> dict:
        return {
            "path": self.path, "size": self.size, "sha256": self.digest,
            "parts": [part.as_dict() for part in self.parts],
        }


@dataclass(frozen=True)
class Manifest:
    files: tuple[File, ...]

    def as_dict(self) -> dict:
        return {"version": 1, "files": [entry.as_dict() for entry in self.files]}

    def text(self) -> str:
        return canonical(self.as_dict())

    def fingerprint(self) -> str:
        return digest_bytes(self.text().encode("utf-8"))

    def unique_parts(self) -> dict[str, int]:
        return {part.digest: part.size for entry in self.files for part in entry.parts}

    def file(self, path: str) -> File | None:
        return next((entry for entry in self.files if entry.path == path), None)


def parse_manifest(value: object) -> Manifest:
    manifest = check_fields(value, {"version", "files"}, "manifest")
    integer(manifest["version"], 1, 1, "manifest version")
    raw_files = manifest["files"]
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_FILES:
        raise invalid("Invalid manifest file count", maximum=MAX_FILES)
    files = []
    names = set()
    prefixes = set()
    spellings = {}
    sizes = {}
    references = 0
    total = 0
    for raw_file in raw_files:
        record = check_fields(raw_file, {"path", "size", "sha256", "parts"}, "file")
        path = archive_path(record["path"])
        size = integer(record["size"], 0, MAX_ARCHIVE, "file size")
        digest = sha256(record["sha256"])
        parts = record["parts"]
        if not isinstance(parts, list) or len(parts) > MAX_PARTS - references:
            raise invalid("Invalid manifest part count", maximum=MAX_PARTS, path=path)
        references += len(parts)
        total += size
        if total > MAX_ARCHIVE:
            raise invalid("Archive exceeds its logical byte limit", maximum=MAX_ARCHIVE)
        folded = path.casefold()
        if folded in names or folded in prefixes:
            raise invalid("Archive paths overlap", path=path)
        names.add(folded)
        for prefix in PurePosixPath(path).parents:
            if prefix == PurePosixPath("."):
                break
            spelling = prefix.as_posix()
            key = spelling.casefold()
            if key in names or spellings.setdefault(key, spelling) != spelling:
                raise invalid("Archive directory paths overlap", path=path)
            prefixes.add(key)
        normalized = []
        for raw_part in parts:
            part = check_fields(raw_part, {"sha256", "size"}, "part")
            part_digest = sha256(part["sha256"])
            part_size = integer(part["size"], 1, MAX_PART, "part size")
            if sizes.setdefault(part_digest, part_size) != part_size:
                raise invalid("A part digest cannot declare multiple sizes", digest=part_digest)
            normalized.append(Part(part_digest, part_size))
        if size != sum(part.size for part in normalized):
            raise invalid("File size does not match ordered part lengths", path=path)
        if not normalized and digest != digest_bytes(b""):
            raise invalid("Empty files must use the empty SHA-256 digest", path=path)
        files.append(File(path, size, digest, tuple(normalized)))
    return Manifest(tuple(sorted(files, key=lambda entry: entry.path)))


def parse_stored(text: str, fingerprint: str) -> Manifest:
    manifest = parse_manifest(json.loads(text))
    if manifest.text() != text or manifest.fingerprint() != fingerprint:
        raise RuntimeError("Stored manifest is inconsistent with its catalog identity")
    return manifest
