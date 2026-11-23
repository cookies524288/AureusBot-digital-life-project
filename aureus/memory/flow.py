"""合并带原发生时间的长期原件与已索引的本地经历。"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from aureus.memory.history import MEDIA_ASSET_TABLES, MemorySource, MemoryStatus


@dataclass(frozen=True)
class MemoryFlowEntry:
    """原件或本地时间线条目的当前正文与出处。"""

    kind: Literal["history", "live"]
    text: str
    actor_label: str
    recorded_at: datetime | None
    source: MemorySource | None
    record_id: int | None
    time_precision: str | None
    status: MemoryStatus
    is_recallable: bool


def _timeline_epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def browse_flow(
    conn: sqlite3.Connection, now: datetime, offset: int, limit: int
) -> list[MemoryFlowEntry]:
    """按发生时间降序合并，缺失时间排在末尾。"""
    conn.create_function("timeline_epoch", 1, _timeline_epoch, deterministic=True)
    rows = conn.execute(
        """SELECT 'history' AS kind, history.id AS entry_id, source_key, actor_label,
                  coalesce(edit.edited_content, content) AS text, recorded_at AS happened,
                  time_precision, status,
                  (status = 'active' AND (valid_from IS NULL OR valid_from <= ?)
                   AND (valid_to IS NULL OR valid_to > ?) AND coalesce(edit.is_enabled, 1))
                    AS is_recallable
           FROM memory_history history LEFT JOIN memory_edits edit
             ON edit.kind = 'history' AND edit.entry_id = history.id
           WHERE json_extract(source_key, '$[2]') NOT IN (?, ?, ?, ?)
           UNION ALL
           SELECT 'live', memory_index.record_id, NULL, actor_label,
                  coalesce(edit.edited_content, content), timeline_epoch(recorded_at), NULL,
                  'active', coalesce(edit.is_enabled, 1)
           FROM memory_index LEFT JOIN memory_edits edit
             ON edit.kind = 'live' AND edit.entry_id = memory_index.rowid
           ORDER BY happened DESC, kind, entry_id DESC LIMIT ? OFFSET ?""",
        (now.timestamp(), now.timestamp(), *MEDIA_ASSET_TABLES, limit, offset),
    )
    return [
        MemoryFlowEntry(
            row["kind"],
            row["text"],
            row["actor_label"],
            datetime.fromtimestamp(row["happened"], UTC) if row["happened"] is not None else None,
            MemorySource.decode(row["source_key"]) if row["source_key"] is not None else None,
            row["entry_id"] if row["kind"] == "live" else None,
            row["time_precision"],
            row["status"],
            bool(row["is_recallable"]),
        )
        for row in rows
    ]
