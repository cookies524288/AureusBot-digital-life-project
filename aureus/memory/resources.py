"""文字记忆的统一维护目录；按业务类型浏览，不区分写入原件的路径。"""

import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from aureus.memory.edits import MemoryKind
from aureus.memory.history import decode_history_row
from aureus.memory.removal_operations import append_removal_operation

PICTURE_ARCHIVE = """
    SELECT 'archive', history.id, history.recorded_at, history.content, history.source_key,
           history.status, NULL, NULL, NULL, NULL,
           (SELECT substr(value ->> '$.reference', 13)
            FROM json_each(history.evidence)
            WHERE value ->> '$.kind' = 'attachment_archived'
              AND length(value ->> '$.reference') = 76
              AND substr(value ->> '$.reference', 1, 12) = 'attachments/'
              AND substr(value ->> '$.reference', 13) NOT GLOB '*[^0-9a-f]*'
            LIMIT 1)
    FROM memory_history history
    WHERE json_extract(history.source_key, '$[2]') = 'image_assets'
"""

_TEXT_CATALOG = """
WITH catalog AS (
    SELECT 'history' AS kind, history.id AS entry_id, history.content, history.actor_label,
           history.recorded_at, history.source_key,
           CASE
               WHEN json_extract(history.source_key, '$[2]') IN ('paragraphs', 'relations',
                    'fact_claims', 'episodes')
               THEN json_extract(history.source_key, '$[2]')
               WHEN json_extract(history.source_key, '$[2]') IN ('mai_messages', 'messages')
               THEN 'messages'
           END AS category,
           history.status, edit.edited_content, coalesce(edit.is_enabled, 1) AS is_enabled,
           coalesce(edit.is_removed, 0) AS is_removed, NULL AS original_id,
           (history.status = 'active'
            AND (history.valid_from IS NULL OR history.valid_from <= :now)
            AND (history.valid_to IS NULL OR history.valid_to > :now)
            AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)) AS is_recallable
    FROM memory_history history
    LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = history.id
    UNION ALL
    SELECT 'live', live.rowid, live.content, live.actor_label,
           unixepoch(live.recorded_at, 'subsec'), NULL,
           CASE event.event_type WHEN 'message' THEN 'messages' ELSE 'experiences' END,
           'active', edit.edited_content, coalesce(edit.is_enabled, 1),
           coalesce(edit.is_removed, 0), CASE WHEN live.rowid = event.id THEN event.id END,
           (coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0))
    FROM memory_index live
    JOIN timeline event ON event.id = live.record_id
    LEFT JOIN memory_edits edit ON edit.kind = 'live' AND edit.entry_id = live.rowid
)
"""
_MATCHING = (
    _TEXT_CATALOG
    + """
, matching AS (
    SELECT * FROM catalog
    WHERE category IS NOT NULL AND (:category = '' OR category = :category)
      AND is_removed = :removed
      AND (:status = '' OR status = :status)
      AND (:recall = '' OR is_recallable = (:recall = 'enabled'))
      AND (:query = '' OR instr(lower(coalesce(edited_content, content)), lower(:query)) > 0
                      OR instr(lower(content), lower(:query)) > 0
                      OR instr(lower(actor_label), lower(:query)) > 0
                      OR instr(lower(source_key), lower(:query)) > 0)
)
"""
)


def browse_text_resources(
    connection: sqlite3.Connection,
    query: str,
    category: str,
    status: str,
    recall: str,
    offset: int,
    limit: int,
    now: datetime,
    *,
    removed: bool = False,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """同一数据库快照返回文字目录、准确总数和当前匹配的类型计数。"""
    params = {
        "query": query,
        "category": category,
        "status": status,
        "recall": recall,
        "offset": offset,
        "limit": limit,
        "now": now.timestamp(),
        "removed": removed,
    }
    with connection:
        connection.execute("BEGIN")
        counts = dict(
            connection.execute(
                _MATCHING + "SELECT category, count(*) FROM matching GROUP BY category", params
            )
        )
        rows = connection.execute(
            _MATCHING
            + "SELECT * FROM matching ORDER BY recorded_at DESC NULLS LAST, kind, entry_id DESC"
            + " LIMIT :limit OFFSET :offset",
            params,
        ).fetchall()
        return [_text_resource(connection, row) for row in rows], sum(counts.values()), counts


def _text_resource(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    entry_id = row["entry_id"]
    text = row["edited_content"] if row["edited_content"] is not None else row["content"]
    original = row["content"] if row["edited_content"] is not None else None
    if row["kind"] == "history":
        stored = connection.execute(
            "SELECT * FROM memory_history WHERE id = ?", (entry_id,)
        ).fetchone()
        resource = asdict(replace(decode_history_row(stored), text=text, original_text=original))
    else:
        stamp = row["recorded_at"]
        resource = {
            "record_id": entry_id,
            "recorded_at": datetime.fromtimestamp(stamp, UTC) if stamp is not None else None,
            "actor_label": row["actor_label"],
            "text": text,
            "original_text": original,
        }
    return resource | {
        "kind": row["kind"],
        "entry_id": entry_id,
        "labels": [row["category"]],
        "is_enabled": bool(row["is_enabled"]),
        "is_removed": bool(row["is_removed"]),
        "is_recallable": bool(row["is_recallable"]),
    }


def set_resources_removed(
    connection: sqlite3.Connection,
    targets: Sequence[tuple[MemoryKind, int]],
    removed: bool,
    now: datetime,
    *,
    record_operation: bool,
) -> bool:
    """仅更改找到且仍在预期列表中的条目；选中内容已变化则整批不写。"""
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("请选择不重复的文字记忆。")
    pairs = ", ".join(f"(:kind{index}, :entry{index})" for index in range(len(targets)))
    params: dict[str, str | int | float] = {"now": now.timestamp(), "prior": not removed}
    for index, (kind, entry_id) in enumerate(targets):
        params[f"kind{index}"] = kind
        params[f"entry{index}"] = entry_id
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        matched = connection.execute(
            _TEXT_CATALOG
            + "SELECT original_id FROM catalog WHERE category IS NOT NULL "
            + f"AND is_removed = :prior AND (kind, entry_id) IN (VALUES {pairs})",
            params,
        ).fetchall()
        if len(matched) != len(targets):
            return False
        connection.executemany(
            """INSERT INTO memory_edits(kind, entry_id, is_removed) VALUES (?, ?, ?)
               ON CONFLICT(kind, entry_id) DO UPDATE SET is_removed = excluded.is_removed""",
            [(kind, entry_id, removed) for kind, entry_id in targets],
        )
        if record_operation:
            append_removal_operation(
                connection, removed, now, [row[0] for row in matched if row[0] is not None]
            )
    return True
