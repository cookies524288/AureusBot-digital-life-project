"""长期记忆流的事件投影、筛选及分页。"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from aureus.memory.history import MemorySnapshotConflictError, MemorySource

type MemoryEventType = Literal[
    "profile_created",
    "profile_updated",
    "profile_snapshot",
    "profile_override_set",
    "person_merged",
    "relation_frozen",
    "delete_executed",
    "delete_restored",
]


@dataclass(frozen=True)
class MemoryEvent:
    """由原件或本地实际操作支持的变动摘要。"""

    event_type: MemoryEventType
    recorded_at: datetime | None
    summary: str
    conversations: tuple[str, ...] = ()
    source: MemorySource | None = None


def open_flow(conn: sqlite3.Connection) -> None:
    """建立迁入事件表，其内容可由同一已校验快照重建。"""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_imported_events (
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                happened REAL,
                summary TEXT NOT NULL,
                conversations TEXT NOT NULL,
                PRIMARY KEY (source, event_type)
            )
        """)


def publish_events(
    conn: sqlite3.Connection, events: Iterable[MemoryEvent], snapshot: tuple[str, str]
) -> int:
    """仅从已发布的同一母本追加真实事件，返回新登记数。"""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        match = conn.execute(
            "SELECT fingerprint FROM memory_history_snapshots WHERE instance = ?", snapshot[:1]
        ).fetchone()
        if match is None or match[0] != snapshot[1]:
            raise MemorySnapshotConflictError
        written = 0
        for event in events:
            if event.source is None or event.source.instance != snapshot[0]:
                raise ValueError("迁入事件缺少对应的原来源")
            written += conn.execute(
                """INSERT INTO memory_imported_events VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source, event_type) DO NOTHING""",
                (
                    event.source.encode(),
                    event.event_type,
                    event.recorded_at.timestamp() if event.recorded_at else None,
                    event.summary,
                    json.dumps(event.conversations, ensure_ascii=False),
                ),
            ).rowcount
    return written


def browse_flow(
    conn: sqlite3.Connection,
    offset: int,
    limit: int,
    *,
    event_type: MemoryEventType | None,
    conversation: str,
    start: datetime | None,
    end: datetime | None,
) -> tuple[list[MemoryEvent], list[str]]:
    """在分页前筛选真实事件，返回事件和有明确归属的会话。"""
    conn.execute("BEGIN")
    people = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name = 'people_memory_events'"
    ).fetchone()
    select_events = """SELECT source, event_type, happened, summary, conversations
                       FROM memory_imported_events"""
    if people:
        select_events += """
            UNION ALL SELECT source, event_type, happened, summary, conversations
            FROM people_memory_events AS people
            WHERE NOT EXISTS (
                SELECT 1 FROM memory_imported_events AS imported
                WHERE imported.source = people.source AND imported.event_type = people.event_type
            )
        """
    rows = conn.execute(
        f"""SELECT * FROM ({select_events}) AS flow
            WHERE (:event_type IS NULL OR event_type = :event_type)
              AND (:conversation = '' OR EXISTS (
                  SELECT 1 FROM json_each(flow.conversations) WHERE value = :conversation
              ))
              AND (:start IS NULL OR happened >= :start)
              AND (:end IS NULL OR happened <= :end)
            ORDER BY happened DESC, event_type, source, summary LIMIT :limit OFFSET :offset""",
        {
            "event_type": event_type,
            "conversation": conversation,
            "offset": offset,
            "limit": limit,
            "start": start.timestamp() if start else None,
            "end": end.timestamp() if end else None,
        },
    )
    items = [
        MemoryEvent(
            row["event_type"],
            datetime.fromtimestamp(row["happened"], UTC) if row["happened"] is not None else None,
            row["summary"],
            tuple(json.loads(row["conversations"])),
            MemorySource.decode(row["source"]) if row["source"] else None,
        )
        for row in rows
    ]
    conversations = conn.execute(
        """SELECT DISTINCT value FROM memory_imported_events, json_each(conversations)
           ORDER BY value"""
    )
    return items, [row[0] for row in conversations]
