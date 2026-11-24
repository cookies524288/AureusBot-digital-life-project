"""事实与非图形关系的版本化检修；保留来源、覆盖与操作记录。"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from sqlite3 import Connection
from typing import Literal
from uuid import uuid4

from aureus.memory.edits import MAX_MEMORY_TEXT
from aureus.memory.history import MemorySource
from aureus.memory.summary_store import EvidenceCheck, ScopeCheck, summary_detail

type SummaryAccess = tuple[ScopeCheck, EvidenceCheck]
type LifecycleAction = Literal[
    "correct", "enable", "disable", "freeze", "unfreeze", "protect", "unprotect", "remove", "restore"
]

_PREVIEW_TTL = timedelta(minutes=15)


class LifecycleConflictError(ValueError):
    """来源、计划版本或当前状态不允许执行，未改动原件或覆盖。"""


@dataclass(frozen=True)
class LifecycleState:
    """当前覆盖；缺省字段不改变原件。"""

    edited_content: str | None = None
    is_enabled: bool = True
    is_removed: bool = False
    is_frozen: bool = False
    is_protected: bool = False


@dataclass(frozen=True)
class LifecycleTarget:
    """在同一快照中按完整键解析的检修对象。"""

    kind: Literal["history", "summary"]
    entry_id: int
    state: LifecycleState
    version: str
    text: str
    is_current: bool


@dataclass(frozen=True)
class LifecycleOperation:
    """已执行操作的审计摘要；不重复保存或呈现证据原文。"""

    plan_id: str
    action: str
    applied_at: str
    undone_at: str | None
    can_undo: bool


def open_lifecycle(connection: Connection) -> None:
    """建立版本化的预览、应用与撤销持久记录。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE IF NOT EXISTS memory_lifecycle_relations (
            source_key TEXT PRIMARY KEY, is_frozen INTEGER NOT NULL, is_protected INTEGER NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS memory_lifecycle_plans (
            id TEXT PRIMARY KEY, source_key TEXT NOT NULL, action TEXT NOT NULL,
            created_at TEXT NOT NULL, expected TEXT NOT NULL, previous TEXT NOT NULL,
            proposed TEXT NOT NULL, applied_at TEXT, applied_version TEXT, undone_at TEXT
        )""")
        connection.execute("""CREATE INDEX IF NOT EXISTS memory_lifecycle_plan_source
            ON memory_lifecycle_plans (source_key, created_at DESC)""")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _target(
    connection: Connection, source: MemorySource, now: datetime, access: SummaryAccess | None
) -> LifecycleTarget | None:
    if source.table == "summary_facts":
        detail = summary_detail(connection, source, *access) if access is not None else None
        if detail is None or detail.entry_id is None:
            return None
        kind = "summary"
        entry_id = detail.entry_id
        original = connection.execute(
            "SELECT text, evidence, superseded_by FROM memory_summary_facts WHERE id=?", (entry_id,)
        ).fetchone()
        current = detail.status == "active"
    elif source.table in ("fact_claims", "relations", "episodes"):
        original = connection.execute(
            "SELECT * FROM memory_history WHERE source_key=?", (source.encode(),)
        ).fetchone()
        if original is None:
            return None
        kind = "history"
        entry_id = original["id"]
        point = now.timestamp()
        current = (
            original["status"] == "active"
            and (original["valid_from"] is None or original["valid_from"] <= point)
            and (original["valid_to"] is None or original["valid_to"] > point)
        )
    else:
        return None
    edit = connection.execute(
        "SELECT * FROM memory_edits WHERE kind=? AND entry_id=?", (kind, entry_id)
    ).fetchone()
    if edit is not None and edit["is_bundle_removed"]:
        return None
    flags = connection.execute(
        "SELECT * FROM memory_lifecycle_relations WHERE source_key=?", (source.encode(),)
    ).fetchone()
    state = LifecycleState(
        edit["edited_content"] if edit is not None else None,
        edit is None or bool(edit["is_enabled"]),
        edit is not None and bool(edit["is_removed"]),
        flags is not None and bool(flags["is_frozen"]),
        flags is not None and bool(flags["is_protected"]),
    )
    latest = connection.execute(
        """SELECT rowid, applied_at, undone_at FROM memory_lifecycle_plans
           WHERE source_key=? AND applied_at IS NOT NULL
           ORDER BY applied_at DESC, rowid DESC LIMIT 1""",
        (source.encode(),),
    ).fetchone()
    version = sha256(_json([list(original), list(edit) if edit else None, asdict(state),
                            list(latest) if latest else None, current]).encode()).hexdigest()
    text = original["text"] if kind == "summary" else original["content"]
    return LifecycleTarget(kind, entry_id, state, version, text, current)


def _propose(
    source: MemorySource, target: LifecycleTarget, action: LifecycleAction, text: str | None
) -> LifecycleState:
    state = target.state
    if not target.is_current:
        raise LifecycleConflictError("原件已失效或没有可用证据，不能应用此更改。")
    if state.is_removed and action != "restore":
        raise LifecycleConflictError("请先恢复已移除的资料。")
    if action == "correct":
        if state.is_frozen or state.is_protected or source.table == "relations":
            raise LifecycleConflictError("该关系或冻结资料不能直接替换正文。")
        content = text.strip() if text is not None else ""
        if not content or len(content) > MAX_MEMORY_TEXT:
            raise ValueError(f"请填写修正正文，最多 {MAX_MEMORY_TEXT} 字。")
        proposed = replace(state, edited_content=content)
    elif action in ("enable", "disable"):
        if state.is_frozen or state.is_protected:
            raise LifecycleConflictError("请先解除关系的冻结或保护。")
        proposed = replace(state, is_enabled=action == "enable")
    elif action in ("remove", "restore"):
        if action == "remove" and (state.is_frozen or state.is_protected):
            raise LifecycleConflictError("请先解除关系的冻结或保护。")
        proposed = replace(state, is_removed=action == "remove")
    elif source.table != "relations":
        raise ValueError("冻结和保护只用于关系。")
    elif action in ("freeze", "unfreeze"):
        proposed = replace(state, is_frozen=action == "freeze")
    else:
        proposed = replace(state, is_protected=action == "protect")
    if proposed == state:
        raise LifecycleConflictError("资料已处于目标状态。")
    return proposed


def _view(plan: object, source: MemorySource, state: LifecycleState) -> dict[str, object]:
    return {"source": asdict(source), "state": asdict(state), "plan_id": plan}


def preview_change(
    connection: Connection, source: MemorySource, action: LifecycleAction, text: str | None,
    now: datetime, access: SummaryAccess | None, session_active: Callable[[], bool],
) -> dict[str, object] | None:
    """预览当前及拟议影响，记住版本，不写覆盖。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        target = _target(connection, source, now, access)
        if target is None or not session_active():
            return None
        proposed = _propose(source, target, action, text)
        identifier = uuid4().hex
        connection.execute(
            "DELETE FROM memory_lifecycle_plans WHERE applied_at IS NULL AND created_at < ?",
            ((now - _PREVIEW_TTL).isoformat(),),
        )
        connection.execute(
            """INSERT INTO memory_lifecycle_plans
               (id, source_key, action, created_at, expected, previous, proposed)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (identifier, source.encode(), action, now.isoformat(), target.version,
             _json(asdict(target.state)), _json(asdict(proposed))),
        )
        return _view(identifier, source, proposed) | {
            "before": asdict(target.state), "expires_at": now + _PREVIEW_TTL,
            "action": action, "original_version": target.version,
            "before_text": target.state.edited_content or target.text,
            "after_text": proposed.edited_content or target.text,
        }


def operation_history(
    connection: Connection, source: MemorySource, now: datetime, access: SummaryAccess | None,
    session_active: Callable[[], bool], offset: int, limit: int,
) -> tuple[list[LifecycleOperation], int] | None:
    """按单项原件查审计，复验可见性后才返回计数。"""
    with connection:
        connection.execute("BEGIN")
        if _target(connection, source, now, access) is None or not session_active():
            return None
        selection = "FROM memory_lifecycle_plans WHERE source_key=? AND applied_at IS NOT NULL"
        key = (source.encode(),)
        total = connection.execute(f"SELECT count(*) {selection}", key).fetchone()[0]
        rows = connection.execute(
            f"SELECT * {selection} ORDER BY applied_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*key, limit, offset),
        ).fetchall()
        current = _target(connection, source, now, access)
        if current is None or not session_active():
            return None
        return [
            LifecycleOperation(row["id"], row["action"], row["applied_at"], row["undone_at"],
                               current.is_current and row["undone_at"] is None
                               and row["applied_version"] == current.version)
            for row in rows
        ], total


def _write(
    connection: Connection, source: MemorySource, target: LifecycleTarget, state: LifecycleState
) -> None:
    connection.execute(
        """INSERT INTO memory_edits(kind, entry_id, edited_content, is_enabled, is_removed)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind, entry_id) DO UPDATE SET
           edited_content=excluded.edited_content, is_enabled=excluded.is_enabled,
           is_removed=excluded.is_removed""",
        (target.kind, target.entry_id, state.edited_content, state.is_enabled, state.is_removed),
    )
    if source.table == "relations":
        connection.execute(
            """INSERT INTO memory_lifecycle_relations VALUES (?, ?, ?)
               ON CONFLICT(source_key) DO UPDATE SET
               is_frozen=excluded.is_frozen, is_protected=excluded.is_protected""",
            (source.encode(), state.is_frozen, state.is_protected),
        )


def change_plan(
    connection: Connection, identifier: str, now: datetime, access: SummaryAccess | None,
    session_active: Callable[[], bool], *, undo: bool,
) -> dict[str, object] | None:
    """确认或撤销时，同一写事务复核会话、原件、状态及应用版本。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if not session_active():
            return None
        plan = connection.execute(
            "SELECT * FROM memory_lifecycle_plans WHERE id=?", (identifier,)
        ).fetchone()
        if plan is None:
            return None
        source = MemorySource.decode(plan["source_key"])
        target = _target(connection, source, now, access)
        if target is None:
            return None
        if undo:
            expected = plan["applied_version"] if plan["undone_at"] is None else None
        else:
            fresh = now < datetime.fromisoformat(plan["created_at"]) + _PREVIEW_TTL
            expected = plan["expected"] if plan["applied_at"] is None and fresh else None
        if not target.is_current or target.version != expected:
            raise LifecycleConflictError("原件、权限或版本已变化，请重新预览；未覆盖当前资料。")
        state = LifecycleState(**json.loads(plan["previous" if undo else "proposed"]))
        _write(connection, source, target, state)
        timestamp = "undone_at" if undo else "applied_at"
        connection.execute(
            f"UPDATE memory_lifecycle_plans SET {timestamp}=? WHERE id=?", (now.isoformat(), identifier)
        )
        current = _target(connection, source, now, access)
        if current is None or not current.is_current or not session_active():
            raise LifecycleConflictError("当前权限已变化，未保存更改。")
        if not undo:
            connection.execute(
                "UPDATE memory_lifecycle_plans SET applied_version=? WHERE id=?",
                (current.version, identifier),
            )
        return _view(identifier, source, state) | {"status": "undone" if undo else "applied"}
