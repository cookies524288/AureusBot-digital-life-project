"""只读核对本地文字索引的状态和可见标签。"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from aureus.common.sqlite import SQLITE_BUSY_TIMEOUT_SECONDS
from aureus.timeline import VisibilityScope


@dataclass(frozen=True)
class LiveMemoryInspection:
    """仅计入时间线原件确认可见的索引条目；各项原因可重叠。"""

    checked: int
    disabled: int
    removed: int
    visibility_mismatch: int


def inspect_live_memory(database: Path, visibility: VisibilityScope) -> LiveMemoryInspection:
    """从单次只读查询取得统计，不打开或重建记忆子系统。"""
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)) as connection:
        row = connection.execute(
            """SELECT count(*),
                      count(*) FILTER (WHERE edit.is_enabled = 0),
                      count(*) FILTER (WHERE edit.is_removed = 1),
                      count(*) FILTER (WHERE entry.visibility IS NOT event.visibility)
               FROM memory_index entry
               JOIN timeline event ON event.id = entry.record_id AND event.id = entry.rowid
               LEFT JOIN memory_edits edit ON edit.kind = 'live' AND edit.entry_id = event.id
               WHERE event.visibility = ?""",
            (visibility.value,),
        ).fetchone()
    return LiveMemoryInspection(*row)
