"""文字记忆移除与恢复的真实操作记录（D99）。"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from aureus.common.sqlite import SQLITE_BUSY_TIMEOUT_SECONDS
from aureus.timeline import VisibilityScope

_VISIBLE_OPERATIONS = """
    WITH visible_operations AS (
        SELECT operation.id, operation.removed, operation.occurred_at, count(*) AS changed
        FROM memory_removal_operations operation
        JOIN json_each(operation.record_ids) target
        JOIN timeline original ON original.id = target.value
        WHERE original.visibility = :visibility
        GROUP BY operation.id
    )
"""


@dataclass(frozen=True)
class MemoryRemovalOperation:
    """一次批量操作在指定可见范围内的摘要。"""

    kind: Literal["remove", "restore"]
    occurred_at: datetime
    changed: int


def open_removal_operations(connection: sqlite3.Connection) -> None:
    """初始化维护记录表，不从现有状态补造记录。"""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS memory_removal_operations (
            id INTEGER PRIMARY KEY,
            removed INTEGER NOT NULL CHECK (removed IN (0, 1)),
            occurred_at REAL NOT NULL,
            record_ids TEXT NOT NULL
        )
    """)


def append_removal_operation(
    connection: sqlite3.Connection, removed: bool, now: datetime, record_ids: Sequence[int]
) -> None:
    """随维护标记一起提交；仅为可关联原件的本地成员保存定位键。"""
    if record_ids:
        connection.execute(
            "INSERT INTO memory_removal_operations(removed, occurred_at, record_ids) "
            "VALUES (?, ?, ?)",
            (removed, now.timestamp(), json.dumps(record_ids)),
        )


def browse_removal_operations(
    database: Path, visibility: VisibilityScope, offset: int, limit: int
) -> tuple[list[MemoryRemovalOperation], int]:
    """使用同一只读快照，按原件可见范围过滤后分页。"""
    uri = database.resolve().as_uri() + "?mode=ro"
    params = {"visibility": visibility.value, "offset": offset, "limit": limit}
    with closing(sqlite3.connect(uri, uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)) as conn, conn:
        conn.execute("BEGIN")
        total = conn.execute(
            _VISIBLE_OPERATIONS + "SELECT count(*) FROM visible_operations", params
        ).fetchone()[0]
        rows = conn.execute(
            _VISIBLE_OPERATIONS
            + "SELECT removed, occurred_at, changed FROM visible_operations "
            + "ORDER BY occurred_at DESC, id DESC LIMIT :limit OFFSET :offset",
            params,
        )
        return [
            MemoryRemovalOperation(
                "remove" if removed else "restore", datetime.fromtimestamp(stamp, UTC), changed
            )
            for removed, stamp, changed in rows
        ], total
