"""文字记忆的统一维护目录；按业务类型浏览，不区分写入原件的路径。"""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from aureus.memory.edits import MemoryKind
from aureus.memory.history import MemorySource, decode_history_row
from aureus.memory.message_boundary import is_message_event, is_message_source
from aureus.memory.removal_operations import append_removal_operation
from aureus.memory.summary_store import (
    EvidenceCheck,
    ScopeCheck,
    SummaryVisibilityChangedError,
    authorized_summary_scopes,
    summary_catalog,
    summary_detail,
)

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
    LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = history.id
    WHERE json_extract(history.source_key, '$[2]') = 'image_assets'
      AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)
      AND NOT coalesce(edit.is_bundle_removed, 0)
"""

_TEXT_CATALOG = f"""
WITH catalog AS (
    SELECT 'history' AS kind, history.id AS entry_id, history.content, history.actor_label,
           history.recorded_at, history.source_key,
           CASE
               WHEN json_extract(history.source_key, '$[2]') IN ('paragraphs', 'relations',
                    'fact_claims', 'episodes')
               THEN json_extract(history.source_key, '$[2]')
           END AS category,
           history.status, edit.edited_content, coalesce(edit.is_enabled, 1) AS is_enabled,
           coalesce(edit.is_removed, 0) AS is_removed, NULL AS original_id,
           (history.status = 'active'
            AND (history.valid_from IS NULL OR history.valid_from <= :now)
            AND (history.valid_to IS NULL OR history.valid_to > :now)
            AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)) AS is_recallable
    FROM memory_history history
    LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = history.id
    WHERE NOT coalesce(edit.is_bundle_removed, 0)
      AND NOT ({is_message_source("history.source_key")})
    UNION ALL
    SELECT 'live', live.rowid, live.content, live.actor_label,
           unixepoch(live.recorded_at, 'subsec'), NULL,
           'experiences',
           'active', edit.edited_content, coalesce(edit.is_enabled, 1),
           coalesce(edit.is_removed, 0), CASE WHEN live.rowid = event.id THEN event.id END,
           (coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0))
    FROM memory_index live
    JOIN timeline event ON event.id = live.record_id
    LEFT JOIN memory_edits edit ON edit.kind = 'live' AND edit.entry_id = live.rowid
    WHERE NOT ({is_message_event('event.event_type')})
    UNION ALL
    SELECT 'summary', fact.id,
           CASE WHEN current.value ->> '$[1]' = 'active' THEN fact.text ELSE '' END,
           '会话事实', unixepoch(delta.recorded_at, 'subsec'),
           json_array('aureus', 'memory', 'summary_facts', json_array(fact.id)),
           'fact_claims', current.value ->> '$[1]',
           CASE WHEN current.value ->> '$[1]' = 'active' THEN edit.edited_content END,
           coalesce(edit.is_enabled, 1), coalesce(edit.is_removed, 0), NULL,
           (current.value ->> '$[1]' = 'active' AND coalesce(edit.is_enabled, 1)
            AND NOT coalesce(edit.is_removed, 0))
    FROM json_each(:summaries) current JOIN memory_summary_facts fact
        ON fact.id = current.value ->> '$[0]'
    JOIN memory_summary_deltas delta ON delta.id = fact.delta_id
    LEFT JOIN memory_edits edit ON edit.kind = 'summary' AND edit.entry_id = fact.id
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
                      OR (kind != 'summary' AND instr(lower(content), lower(:query)) > 0)
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
    summary_access: tuple[ScopeCheck, EvidenceCheck] | None = None,
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
        access = summary_access if category in ("", "fact_claims") else None
        scopes = authorized_summary_scopes(connection, access[0]) if access else set()
        params["summaries"] = json.dumps(
            summary_catalog(connection, scopes, access[1]) if access else []
        )
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
        resources = [
            resource for row in rows
            if (resource := _text_resource(connection, row, summary_access)) is not None
        ]
        if access and (
            len(resources) != len(rows)
            or authorized_summary_scopes(connection, access[0]) != scopes
        ):
            raise SummaryVisibilityChangedError
        return resources, sum(counts.values()), counts


def _text_resource(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    summary_access: tuple[ScopeCheck, EvidenceCheck] | None,
) -> dict[str, Any] | None:
    entry_id = row["entry_id"]
    text = row["edited_content"] if row["edited_content"] is not None else row["content"]
    original = row["content"] if row["edited_content"] is not None else None
    if row["kind"] == "history":
        stored = connection.execute(
            "SELECT * FROM memory_history WHERE id = ?", (entry_id,)
        ).fetchone()
        resource = asdict(replace(decode_history_row(stored), text=text, original_text=original))
        if row["category"] == "relations":
            flags = connection.execute(
                "SELECT is_frozen, is_protected FROM memory_lifecycle_relations WHERE source_key=?",
                (row["source_key"],),
            ).fetchone()
            resource.update(is_frozen=bool(flags and flags[0]), is_protected=bool(flags and flags[1]))
    elif row["kind"] == "summary":
        detail = (
            summary_detail(connection, MemorySource.decode(row["source_key"]), *summary_access)
            if summary_access else None
        )
        if detail is None or detail.status != row["status"]:
            return None
        resource = asdict(detail)
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
    params: dict[str, str | int | float] = {
        "now": now.timestamp(), "prior": not removed, "summaries": "[]",
    }
    for index, (kind, entry_id) in enumerate(targets):
        params[f"kind{index}"] = kind
        params[f"entry{index}"] = entry_id
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        matched = connection.execute(
            _TEXT_CATALOG
            + "SELECT original_id FROM catalog WHERE category IS NOT NULL "
            + "AND NOT EXISTS (SELECT 1 FROM memory_lifecycle_relations flags "
            + "WHERE flags.source_key = catalog.source_key AND (flags.is_frozen OR flags.is_protected)) "
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
