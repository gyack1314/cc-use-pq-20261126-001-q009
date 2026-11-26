"""按清单顺序重组字节，同时校验每片和完整文件。"""

from collections.abc import Iterator
import hashlib

from strata.runtime.errors import VaultError
from strata.runtime.media import Media
from strata.transfer.manifest import File


def file_blocks(media: Media, entry: File) -> Iterator[bytes]:
    computed = hashlib.sha256()
    for part in entry.parts:
        for block in media.blocks(part.digest, part.size):
            computed.update(block)
            yield block
    if computed.hexdigest() != entry.digest:
        raise VaultError("file_digest_mismatch", "Ordered objects do not match the declared file digest",
                         409, path=entry.path, expected=entry.digest, actual=computed.hexdigest())
