"""追加不包含文件内容的操作记录。"""

import json
from sqlite3 import Connection

from strata.runtime.errors import invalid


def append(connection: Connection, collection: str | None, kind: str, entity: str,
           at: int, **payload: object) -> None:
    connection.execute(
        "INSERT INTO events(collection,kind,entity,at,payload) VALUES(?,?,?,?,?)",
        (collection, kind, entity, at, json.dumps(payload, sort_keys=True)),
    )


def page(connection: Connection, collection: str | None, after: int, limit: int) -> dict:
    if not 1 <= limit <= 200 or not 0 <= after <= 2**63 - 1:
        raise invalid("Event page limits are invalid")
    if collection is None:
        rows = connection.execute(
            "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit + 1)
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM events WHERE collection=? AND id>? ORDER BY id LIMIT ?",
            (collection, after, limit + 1),
        ).fetchall()
    items = []
    for row in rows[:limit]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        items.append(item)
    return {"items": items, "next_after": items[-1]["id"] if len(rows) > limit else None}
