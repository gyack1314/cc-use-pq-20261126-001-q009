"""规范化现场组显式提交的档案变化。

完整清单本身可以确定新增、删除与同路径内容修改；唯独“移动”无法仅凭
路径或字节推断，因此移动必须由现场组以明确记录提交。字节相同且文件名
相同永远不会被系统自动认定为移动。
"""

from dataclasses import dataclass
import json

from strata.transfer.manifest import archive_path, canonical, digest_bytes
from strata.runtime.errors import invalid


_MOVE_OPS = {"move", "rename", "relocate", "moved", "renamed"}
_DELETE_OPS = {"delete", "deleted", "remove", "removed", "rm"}
_ADD_OPS = {"add", "added", "create", "created", "insert", "new"}
_MODIFY_OPS = {"modify", "modified", "change", "changed", "edit", "edited", "update", "updated"}

_FROM_KEYS = ("from", "old_path", "old", "source", "src", "before", "origin", "from_path")
_TO_KEYS = ("to", "new_path", "new", "target", "dest", "destination", "after", "to_path")
_PATH_KEYS = ("path", "file", "entry", "target_path")


@dataclass(frozen=True)
class Move:
    source: str
    target: str

    def as_dict(self) -> dict:
        return {"op": "move", "from": self.source, "to": self.target}


@dataclass(frozen=True)
class ChangeSet:
    moves: tuple[Move, ...]
    records: tuple[dict, ...]

    def text(self) -> str:
        return canonical(list(self.records))

    def fingerprint(self) -> str:
        return digest_bytes(self.text().encode("utf-8"))

    def move_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple((move.source, move.target) for move in self.moves)


def _first_key(record: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in record:
            return record[key]
    return None


def _as_record(item: object) -> dict:
    if isinstance(item, dict):
        return item
    if isinstance(item, (list, tuple)) and len(item) == 2:
        return {"op": "move", "from": item[0], "to": item[1]}
    raise invalid("Change record must be an object or a [from,to] pair", change=item)


def parse_changes(value: object) -> ChangeSet:
    """把多种近义写法归一成有序、去重的变化记录。

    缺省（None）表示没有任何显式变化，绝不触发移动推断。
    """
    if value is None:
        return ChangeSet((), ())
    if isinstance(value, str):
        raise invalid("Changes must be a list of records")
    if not isinstance(value, (list, tuple)):
        raise invalid("Changes must be a list of records")
    moves: list[Move] = []
    records: list[dict] = []
    seen_records: set[str] = set()
    move_sources: set[str] = set()
    move_targets: set[str] = set()
    for raw in value:
        record = _as_record(raw)
        op_value = _first_key(record, ("op", "action", "type", "kind"))
        op = str(op_value).strip().lower() if op_value is not None else ""
        if not op:
            # 没有操作类型但同时给出 from/to，按移动处理。
            op = "move" if _first_key(record, _FROM_KEYS) is not None else ""
        if op in _MOVE_OPS or (not op and _first_key(record, _FROM_KEYS) is not None):
            raw_from = _first_key(record, _FROM_KEYS)
            raw_to = _first_key(record, _TO_KEYS)
            if raw_from is None or raw_to is None:
                raise invalid("Move record requires explicit from and to paths")
            source = archive_path(raw_from)
            target = archive_path(raw_to)
            if source == target:
                raise invalid("Move record must change the archive path", path=source)
            if source in move_sources:
                raise invalid("A source path has more than one move record", path=source)
            if target in move_targets:
                raise invalid("A move target was recorded more than once", path=target)
            move_sources.add(source)
            move_targets.add(target)
            moves.append(Move(source, target))
            normalized = Move(source, target).as_dict()
        elif op in _DELETE_OPS | _ADD_OPS | _MODIFY_OPS:
            raw_path = _first_key(record, _PATH_KEYS)
            if raw_path is None:
                raise invalid(f"{op} record requires a path")
            path = archive_path(raw_path)
            canonical_op = ("delete" if op in _DELETE_OPS else
                            "add" if op in _ADD_OPS else "modify")
            normalized = {"op": canonical_op, "path": path}
        else:
            raise invalid("Unknown change record operation", operation=op_value)
        encoded = canonical(normalized)
        if encoded not in seen_records:
            seen_records.add(encoded)
            records.append(normalized)
    records.sort(key=lambda item: canonical(item))
    moves.sort(key=lambda move: (move.source, move.target))
    return ChangeSet(tuple(moves), tuple(records))


def parse_changes_json(text: str, fingerprint: str | None = None) -> ChangeSet:
    changes = parse_changes(json.loads(text))
    if fingerprint is not None and changes.fingerprint() != fingerprint:
        raise RuntimeError("Stored changes are inconsistent with their catalog identity")
    return changes


def validate_moves(moves: tuple[tuple[str, str], ...], *, base_paths: set[str],
                   candidate_paths: set[str]) -> None:
    """核对显式移动与两份完整清单一致。

    - 源必须存在于共同历史版；
    - 目标必须存在于候选完整清单；
    - 移动后源路径不能仍在候选清单中（否则应是复制而非移动）；
    - 移动源/目标必须一一对应，避免身份歧义。
    """
    for source, target in moves:
        if source not in base_paths:
            raise invalid("Move source is absent from the declared base revision",
                          **{"from": source, "to": target})
        if target not in candidate_paths:
            raise invalid("Move target is absent from the candidate manifest",
                          **{"from": source, "to": target})
        if source in candidate_paths:
            raise invalid("Moved source path must not remain in the candidate manifest",
                          **{"from": source, "to": target})
