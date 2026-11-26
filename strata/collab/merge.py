"""以共同历史版为基准，对已发布一侧与候选完整清单做身份感知的三方合并。

路径本身无法表达移动：候选侧的身份映射只相信显式移动记录
（``from -> to``），绝不会因为摘要相同或文件名一致就把新增/删除配对成
移动。合并规则区分“路径（位置）”与“摘要（内容）”两个维度，因此一侧
移动、另一侧修改内容可以干净合并；无法自动判断的命运冲突与位置冲突则
整理成可供档案员复核的双方来源。
"""

from dataclasses import dataclass, field
from typing import Any

from strata.runtime.errors import conflict, invalid
from strata.transfer.manifest import File, Manifest, canonical, digest_bytes, parse_manifest

HEAD_NEW = "new:head:"
CANDIDATE_NEW = "new:candidate:"


@dataclass(frozen=True)
class State:
    side: str  # base | head | candidate
    identity: str
    present: bool
    path: str | None
    file: File | None

    @property
    def digest(self) -> str | None:
        return self.file.digest if self.file is not None else None

    @property
    def size(self) -> int | None:
        return self.file.size if self.file is not None else None

    def key(self) -> tuple:
        return (self.present, self.path, self.digest)

    def source(self, **extra: Any) -> dict:
        result = {
            "side": self.side,
            "identity": self.identity,
            "present": self.present,
            "path": self.path,
            "sha256": self.digest,
            "size": self.size,
        }
        if self.file is not None:
            result["entry"] = self.file.as_dict()
        result.update(extra)
        return result


def absent(side: str, identity: str) -> State:
    return State(side, identity, False, None, None)


@dataclass
class Conflict:
    type: str  # path_conflict | delete_modify | content_conflict | move_conflict
    identity: str | None
    path: str | None
    base: State | None
    head: State
    candidate: State
    contenders: list[State] = field(default_factory=list)
    deleting_side: str | None = None
    moved: bool = False
    modified: bool = False
    absorbed: bool = False
    key: str = ""
    conflict_hash: str = ""

    def __post_init__(self) -> None:
        stable = {
            "type": self.type,
            "identity": self.identity,
            "path": self.path,
            "deleting_side": self.deleting_side,
            "states": {
                side: None if state is None else {
                    "present": state.present, "path": state.path, "sha256": state.digest,
                }
                for side, state in (
                    ("base", self.base), ("head", self.head), ("candidate", self.candidate)
                )
            },
            "contenders": [
                {"side": item.side, "identity": item.identity,
                 "path": item.path, "sha256": item.digest}
                for item in self.contenders
            ],
        }
        self.conflict_hash = digest_bytes(canonical(stable).encode("utf-8"))
        if not self.key:
            self.key = self.conflict_hash[:32]

    def descriptor(self) -> dict:
        head_source = self.head.source() if self.head.present else None
        candidate_source = self.candidate.source() if self.candidate.present else None
        sources = [item.source() for item in self.contenders if item.present]
        choices = _choices_for(self.type)
        result = {
            "key": self.key,
            "id": self.key,
            "conflict": self.key,
            "type": self.type,
            "code": self.type,
            "kind": self.type,
            "identity": self.identity,
            "path": self.path,
            "choices": choices,
            "options": choices,
            "deleting_side": self.deleting_side,
            "moved": self.moved,
            "modified": self.modified,
            "base": self.base.source() if self.base is not None and self.base.present else None,
            "head": head_source,
            "candidate": candidate_source,
            "sides": {"head": head_source, "candidate": candidate_source},
            "sources": sources,
            "contenders": sources,
            "conflict_sha256": self.conflict_hash,
        }
        if self.type == "delete_modify":
            # 让“删除意图”也成为可审阅的明确一端。
            result["sides"][self.deleting_side or "head"] = {
                "side": self.deleting_side, "present": False, "path": self.path,
                "state": "deleted",
            }
            result["delete_choice"] = "delete"
            result["keep_choice"] = "keep"
        return result


def _choices_for(conflict_type: str) -> list[str]:
    if conflict_type == "delete_modify":
        return ["head", "candidate", "delete", "keep"]
    return ["head", "candidate"]


@dataclass
class Plan:
    base: Manifest
    head: Manifest
    candidate: Manifest
    moves: tuple[tuple[str, str], ...]
    head_moves: tuple[tuple[str, str], ...]
    states: dict[str, tuple[State, State, State]]
    placements: dict[str, File]
    placement_origin: dict[str, dict]
    conflicts: list[Conflict]
    governed: set[str]

    def descriptor(self) -> dict:
        return {
            "clean": not [item for item in self.conflicts if not item.absorbed],
            "conflicts": [item.descriptor() for item in self.conflicts if not item.absorbed],
            "entries": [entry.as_dict() for entry in sorted(
                self.placements.values(), key=lambda item: item.path)],
            "moves": [{"from": source, "to": target} for source, target in self.moves],
        }


def _identity_maps(base: Manifest, head: Manifest, candidate: Manifest,
                   moves: tuple[tuple[str, str], ...],
                   head_moves: tuple[tuple[str, str], ...]):
    base_paths = {entry.path for entry in base.files}
    move_target_to_source = {target: source for source, target in moves}
    head_target_to_source = {target: source for source, target in head_moves}

    head_id_at_path: dict[str, str] = {}
    for entry in head.files:
        if entry.path in head_target_to_source:
            head_id_at_path[entry.path] = head_target_to_source[entry.path]
        elif entry.path in base_paths:
            head_id_at_path[entry.path] = entry.path
        else:
            head_id_at_path[entry.path] = HEAD_NEW + entry.path

    candidate_id_at_path: dict[str, str] = {}
    for entry in candidate.files:
        if entry.path in move_target_to_source:
            candidate_id_at_path[entry.path] = move_target_to_source[entry.path]
        elif entry.path in base_paths:
            candidate_id_at_path[entry.path] = entry.path
        else:
            candidate_id_at_path[entry.path] = CANDIDATE_NEW + entry.path
    return head_id_at_path, candidate_id_at_path


def build_plan(*, base: Manifest, head: Manifest, candidate: Manifest,
               moves: tuple[tuple[str, str], ...] = (),
               head_moves: tuple[tuple[str, str], ...] = ()) -> Plan:
    base_by_path = {entry.path: entry for entry in base.files}
    head_by_path = {entry.path: entry for entry in head.files}
    candidate_by_path = {entry.path: entry for entry in candidate.files}
    head_id_at_path, candidate_id_at_path = _identity_maps(
        base, head, candidate, moves, head_moves)

    ids = set(base_by_path) | set(head_id_at_path.values()) | set(candidate_id_at_path.values())
    states: dict[str, tuple[State, State, State]] = {}
    for identity in ids:
        base_file = base_by_path.get(identity)
        head_path = next((p for p, i in head_id_at_path.items() if i == identity), None)
        candidate_path = next((p for p, i in candidate_id_at_path.items() if i == identity), None)
        b_state = State("base", identity, base_file is not None, identity if base_file else None,
                        base_file) if base_file else absent("base", identity)
        h_file = head_by_path.get(head_path) if head_path is not None else None
        c_file = candidate_by_path.get(candidate_path) if candidate_path is not None else None
        h_state = State("head", identity, h_file is not None, head_path, h_file) \
            if h_file is not None else absent("head", identity)
        c_state = State("candidate", identity, c_file is not None, candidate_path, c_file) \
            if c_file is not None else absent("candidate", identity)
        states[identity] = (b_state, h_state, c_state)

    placements: dict[str, File] = {}
    origins: dict[str, dict] = {}
    conflicts: list[Conflict] = []

    for identity, (b_state, h_state, c_state) in sorted(states.items()):
        placement, conflict, origin = _merge_identity(identity, b_state, h_state, c_state)
        if conflict is not None:
            conflicts.append(conflict)
        elif placement is not None:
            placements[identity] = placement
            origins[identity] = origin

    governed = _detect_path_conflicts(
        conflicts, placements, origins, states, head_by_path, candidate_by_path,
        head_id_at_path, candidate_id_at_path)

    active_conflicts = [item for item in conflicts if not item.absorbed]
    active_conflicts.sort(key=lambda item: (item.path or "", item.type, item.key))
    return Plan(base, head, candidate, moves, head_moves, states, placements,
                origins, active_conflicts, governed)


def _replace_path(entry: File, path: str) -> File:
    if entry.path == path:
        return entry
    return File(path, entry.size, entry.digest, entry.parts)


def _merge_identity(identity: str, base: State, head: State, candidate: State):
    bk, hk, ck = base.key(), head.key(), candidate.key()
    head_changed = hk != bk
    candidate_changed = ck != bk

    def take(state: State, path_origin: str, content_origin: str):
        origin = {"path_from": path_origin, "content_from": content_origin}
        return _replace_path(state.file, state.path), None, origin

    if not head_changed and not candidate_changed:
        if base.present:
            return base.file, None, {"path_from": "base", "content_from": "base"}
        return None, None, {}
    if head_changed and not candidate_changed:
        if head.present:
            return take(head, "head", "head")
        return None, None, {}
    if candidate_changed and not head_changed:
        if candidate.present:
            return take(candidate, "candidate", "candidate")
        return None, None, {}
    if hk == ck:
        if head.present:
            return take(head, "head", "head")
        return None, None, {}

    # 两侧都以不同方式改变了同一身份。
    if not head.present or not candidate.present:
        deleting = "head" if not head.present else "candidate"
        present_state = candidate if deleting == "head" else head
        moved = bool(base.present and present_state.present
                     and present_state.path != base.path)
        modified = bool(base.present and present_state.present
                        and present_state.digest != base.digest)
        conflict_obj = Conflict(
            "delete_modify", identity, base.path if base.present else
            (head.path or candidate.path), base if base.present else None,
            head, candidate, contenders=[present_state] if present_state.present else [],
            deleting_side=deleting, moved=moved, modified=modified,
        )
        return None, conflict_obj, {}

    # 两侧都保留该身份：先合并路径维度。
    base_path = base.path if base.present else None
    head_path_changed = head.path != base_path
    candidate_path_changed = candidate.path != base_path
    if head.path == candidate.path:
        merged_path = head.path
    elif head_path_changed and not candidate_path_changed:
        merged_path = head.path
    elif candidate_path_changed and not head_path_changed:
        merged_path = candidate.path
    else:
        conflict_obj = Conflict(
            "move_conflict", identity, None, base if base.present else None,
            head, candidate, contenders=[head, candidate],
        )
        return None, conflict_obj, {}

    # 再合并内容维度：一侧改内容、另一侧移动时，修改落到移动后的条目。
    head_content_changed = base.present and head.digest != base.digest
    candidate_content_changed = base.present and candidate.digest != base.digest
    if (head_content_changed and candidate_content_changed
            and head.digest != candidate.digest):
        conflict_obj = Conflict(
            "content_conflict", identity, merged_path, base if base.present else None,
            head, candidate, contenders=[head, candidate],
        )
        return None, conflict_obj, {}

    if candidate_content_changed and not head_content_changed:
        winner = candidate
        content_origin = "candidate"
    elif head_content_changed:
        winner = head
        content_origin = "head"
    else:
        winner = candidate
        content_origin = "candidate"
    path_origin = ("candidate" if candidate_path_changed and not head_path_changed
                   else "head" if head_path_changed and not candidate_path_changed
                   else "same")
    entry = _replace_path(winner.file, merged_path)
    return entry, None, {"path_from": path_origin, "content_from": content_origin}


def _detect_path_conflicts(conflicts, placements, origins, states,
                           head_by_path, candidate_by_path,
                           head_id_at_path, candidate_id_at_path) -> set[str]:
    """同一归档位置被不同身份以不同字节占用时，整理成位置冲突。"""
    governed: set[str] = set()
    common_paths = sorted(set(head_by_path) & set(candidate_by_path))
    for path in common_paths:
        head_identity = head_id_at_path[path]
        candidate_identity = candidate_id_at_path[path]
        h_file = head_by_path[path]
        c_file = candidate_by_path[path]
        if head_identity == candidate_identity:
            continue  # 同一身份的内容分歧已由 identity 冲突处理。
        if h_file.digest == c_file.digest:
            # 字节完全一致：即使来源身份不同，也不会形成“不同资料”冲突，
            # 合并为一个确定条目，避免同路径重复。
            if candidate_identity in placements:
                placements.pop(candidate_identity, None)
            elif head_identity in placements:
                placements.pop(head_identity, None)
            continue
        h_state = states[head_identity][1]
        c_state = states[candidate_identity][2]
        path_conflict = Conflict(
            "path_conflict", None, path, None, h_state, c_state,
            contenders=[h_state, c_state],
        )
        conflicts.append(path_conflict)
        governed.add(head_identity)
        governed.add(candidate_identity)
        placements.pop(head_identity, None)
        placements.pop(candidate_identity, None)
        # 若该位置上还缠着“一侧删除基础身份、另一侧移入新身份”的
        # delete_modify，它表达的是同一个位置选择，合并进 path_conflict。
        for other in conflicts:
            if (other is not path_conflict and other.type == "delete_modify"
                    and not other.absorbed and other.path == path):
                other.absorbed = True
                if other.identity is not None:
                    governed.add(other.identity)
                    placements.pop(other.identity, None)
    return governed


def manifest_changes(old: Manifest, new: Manifest,
                     moves: tuple[tuple[str, str], ...] = ()) -> dict:
    """按馆藏 compare 的词汇给出两侧各自的明确档案变化。

    显式移动会从“删除 + 新增”中抽出，单列为 moves；系统不会自行配对移动。
    """
    previous = {entry.path: entry for entry in old.files}
    current = {entry.path: entry for entry in new.files}
    moved_sources = {source for source, _ in moves}
    moved_targets = {target for _, target in moves}
    result = {"added": [], "removed": [], "content_changed": [],
              "layout_changed": [], "moves": []}
    for source, target in moves:
        before, after = previous.get(source), current.get(target)
        result["moves"].append({
            "from": source, "to": target,
            "path": target,
            "content_changed": bool(before and after and before.digest != after.digest),
            "before": before.as_dict() if before else None,
            "after": after.as_dict() if after else None,
        })
    for path in sorted(set(previous) | set(current)):
        before, after = previous.get(path), current.get(path)
        if before is None:
            if path in moved_targets:
                continue
            result["added"].append(after.as_dict())
        elif after is None:
            if path in moved_sources:
                continue
            result["removed"].append(before.as_dict())
        elif before.digest != after.digest:
            result["content_changed"].append(
                {"before": before.as_dict(), "after": after.as_dict()})
        elif before.parts != after.parts:
            result["layout_changed"].append(
                {"before": before.as_dict(), "after": after.as_dict()})
    return result


def normalize_decision(item: object) -> dict:
    if not isinstance(item, dict):
        raise invalid("Each decision must be an object")
    key = item.get("key", item.get("conflict", item.get("id")))
    conflict_type = item.get("type", item.get("kind", item.get("code")))
    path = item.get("path")
    identity = item.get("identity")
    choice = item.get("choice", item.get("resolution", item.get("decision", item.get("select"))))
    if isinstance(choice, dict):
        choice = choice.get("choice") or choice.get("side") or choice.get("value")
    side = item.get("side")
    if choice is None and side is not None:
        choice = side
    if not isinstance(choice, str) or not choice.strip():
        raise invalid("Decision requires a choice", decision=item)
    choice = choice.strip().lower()
    aliases = {
        "take-head": "head", "use_head": "head", "theirs": "head", "published": "head",
        "take-candidate": "candidate", "use_candidate": "candidate", "ours": "candidate",
        "incoming": "candidate", "late": "candidate",
        "remove": "delete", "deleted": "delete", "drop": "delete",
        "retain": "keep", "keep_modification": "keep", "keep-modified": "keep",
    }
    choice = aliases.get(choice.replace("-", "_") if False else choice, choice)
    target_path = item.get("target_path", item.get("destination", item.get("to")))
    reason = item.get("reason", item.get("note", ""))
    if target_path is not None:
        target_path = target_path if isinstance(target_path, str) else None
        if target_path is not None:
            from strata.transfer.manifest import archive_path
            target_path = archive_path(target_path)
    return {
        "key": key if isinstance(key, str) else None,
        "type": conflict_type if isinstance(conflict_type, str) else None,
        "path": path if isinstance(path, str) else None,
        "identity": identity if isinstance(identity, str) else None,
        "choice": choice,
        "target_path": target_path,
        "reason": reason if isinstance(reason, str) else "",
    }


def match_decision(conflict_descriptor: dict, decisions: list[dict]) -> dict | None:
    for decision in decisions:
        key = decision.get("key")
        if key and key in (conflict_descriptor["key"], conflict_descriptor.get("conflict_sha256")):
            return decision
    for decision in decisions:
        if decision.get("type") and decision["type"] != conflict_descriptor["type"]:
            continue
        if decision.get("path") is not None and decision["path"] != conflict_descriptor.get("path"):
            continue
        if (decision.get("identity") is not None
                and decision["identity"] != conflict_descriptor.get("identity")):
            continue
        if decision.get("path") is None and decision.get("identity") is None \
                and decision.get("type") is None:
            continue
        return decision
    return None


def assemble(plan: Plan, raw_decisions: list[dict]) -> Manifest:
    """按审阅决定产出合订清单；缺少决定时抛出 conflict。"""
    decisions = [normalize_decision(item) for item in raw_decisions]
    by_key = {item["key"]: item for item in decisions if item.get("key")}
    unresolved = []
    for runtime_conflict in plan.conflicts:
        descriptor = runtime_conflict.descriptor()
        decision = by_key.get(runtime_conflict.key) or match_decision(descriptor, decisions)
        if decision is None:
            unresolved.append(descriptor)
    if unresolved:
        raise conflict("Merge has unresolved conflicts", conflicts=unresolved)

    entries: dict[str, File] = {}
    for identity, entry in plan.placements.items():
        if identity in plan.governed:
            continue
        entries[identity + "@" + entry.path] = entry

    for runtime_conflict in plan.conflicts:
        descriptor = runtime_conflict.descriptor()
        decision = by_key.get(runtime_conflict.key) or match_decision(descriptor, decisions)
        chosen_entries = _apply(runtime_conflict, decision)
        for index, entry in enumerate(chosen_entries):
            entries[f"decision:{runtime_conflict.key}:{index}:{entry.path}"] = entry

    files = sorted(entries.values(), key=lambda item: item.path)
    raw = {"version": 1, "files": [entry.as_dict() for entry in files]}
    # parse_manifest 重新校验路径可移植性、目录重叠、分片与摘要长度。
    return parse_manifest(raw)


def _apply(item: Conflict, decision: dict) -> list[File]:
    choice = decision["choice"]
    target_path = decision.get("target_path")
    if item.type == "path_conflict":
        contender = _side_state(item, choice)
        if contender is None or not contender.present:
            raise invalid("Path conflict decision must name a present source",
                          key=item.key, choice=choice)
        chosen = contender.file
        result = [_replace_path(chosen, target_path or item.path)]
        if target_path and target_path != item.path:
            other = next((state for state in item.contenders
                          if state.side != contender.side and state.present), None)
            if other is not None:
                result.append(other.file)
        return result

    if item.type == "delete_modify":
        if choice == "delete":
            return []
        if choice == "keep":
            keeper = next((state for state in item.contenders if state.present), None)
            if keeper is None:
                raise invalid("There is no retained source to keep", key=item.key)
            return [keeper.file]
        side_state = item.head if choice == "head" else item.candidate if choice == "candidate" else None
        if side_state is None:
            raise invalid("Unknown choice for delete/modify conflict",
                          key=item.key, choice=choice)
        return [side_state.file] if side_state.present else []

    # content_conflict / move_conflict
    side_state = item.head if choice == "head" else item.candidate if choice == "candidate" else None
    if side_state is None or not side_state.present:
        raise invalid("Conflict decision must choose head or candidate",
                      key=item.key, choice=choice)
    entry = side_state.file
    if item.type == "content_conflict" and item.path is not None:
        entry = _replace_path(entry, item.path)
    if target_path:
        entry = _replace_path(entry, target_path)
    return [entry]


def _side_state(item: Conflict, choice: str) -> State | None:
    if choice in ("head", "published"):
        return next((s for s in item.contenders if s.side == "head"), item.head)
    if choice in ("candidate", "incoming", "late"):
        return next((s for s in item.contenders if s.side == "candidate"), item.candidate)
    # 也允许按身份或摘要指定。
    for state in item.contenders:
        if choice == state.identity or choice == state.digest:
            return state
    return None
