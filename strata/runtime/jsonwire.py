"""在交换归档清单时拒绝有歧义的序列化数据。"""

from collections.abc import Iterable
import json


def _unique_fields(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError(f"Non-finite JSON value: {value}")


def _check_strings(value: object) -> None:
    work = [value]
    while work:
        item = work.pop()
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, list):
            work.extend(item)
        elif isinstance(item, dict):
            work.extend(item.keys())
            work.extend(item.values())


def read_json(raw: bytes) -> object:
    value = json.loads(raw.decode("utf-8", errors="strict"),
                       object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
    _check_strings(value)
    return value
