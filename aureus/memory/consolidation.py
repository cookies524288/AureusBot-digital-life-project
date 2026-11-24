"""睡眠整理的原件游标、去重和派生候选。

淡化公式适配自 A_Memorix 2.0.0 的
core/utils/memory_lifecycle_policy.py::retention_at（AGPL-3.0）。
权重只用于派生候选，原件仍可通过全文索引取得。
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from itertools import chain, groupby
from typing import Literal

from aureus.memory.message_boundary import MESSAGE_EVENT_TYPE, is_message_excerpt, is_message_source

type CandidateKind = Literal["experience", "fact", "preference"]


def retention_weight(now: float, last_seen: float, half_life_days: float) -> float:
    """按 A_Memorix 的半衰期公式计算候选在查询时刻的权重。"""
    elapsed_days = max(0.0, now - last_seen) / 86400
    return 2.0 ** (-elapsed_days / half_life_days)


@dataclass(frozen=True)
class ConsolidationItem:
    """单条候选材料及可读回原文的指针。"""

    reference: str
    scope: tuple[str, ...]
    text: str
    timestamp: float | None
    can_extract: bool = False


@dataclass(frozen=True)
class ConsolidationBatch:
    """工作开始和结束时的两个游标。"""

    start: tuple[int, int]
    end: tuple[int, int]
    items: tuple[ConsolidationItem, ...]


@dataclass(frozen=True)
class DerivedCandidate:
    """仍需核查证据的整理结果。"""

    kind: CandidateKind
    text: str
    evidence: tuple[str, ...]
    weight: float


def open_consolidation(conn: sqlite3.Connection) -> None:
    """初始化候选和游标。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_consolidation_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            history_cursor INTEGER NOT NULL, timeline_cursor INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO memory_consolidation_state VALUES (1, 0, 0);
        CREATE TABLE IF NOT EXISTS memory_candidates (
            fingerprint TEXT PRIMARY KEY,
            kind TEXT NOT NULL, content TEXT NOT NULL, last_seen REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memory_candidates_recency
        ON memory_candidates(last_seen DESC, fingerprint);
        CREATE TABLE IF NOT EXISTS memory_candidate_evidence (
            fingerprint TEXT NOT NULL, reference TEXT NOT NULL,
            PRIMARY KEY(fingerprint, reference)
        );
    """)
    # 已部署的整理库可能已有建议。回填和维护触发器在同一事务里就绪。
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memory_extracted_fts'"
        ).fetchone()
        if exists is None:
            conn.execute(
                "CREATE VIRTUAL TABLE memory_extracted_fts USING fts5(content, tokenize='trigram')"
            )
            conn.execute(
                "INSERT INTO memory_extracted_fts(rowid, content) "
                "SELECT rowid, content FROM memory_candidates "
                "WHERE kind IN ('fact', 'preference')"
            )
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_extracted_insert AFTER INSERT ON memory_candidates
            WHEN new.kind IN ('fact', 'preference') BEGIN
                INSERT INTO memory_extracted_fts(rowid, content) VALUES (new.rowid, new.content);
            END
        """)


def _cursor(conn: sqlite3.Connection) -> tuple[int, int]:
    row = conn.execute(
        "SELECT history_cursor, timeline_cursor FROM memory_consolidation_state WHERE id = 1"
    ).fetchone()
    return row[0], row[1]


def read_batch(conn: sqlite3.Connection, size: int) -> ConsolidationBatch | None:
    """从新经历和历史原件各按行号增量取材。"""
    conn.execute("BEGIN")
    start = _cursor(conn)
    timeline = conn.execute(
        f"""
        SELECT m.rowid, t.id, t.timestamp, t.actor_type, t.actor_id,
               t.visibility, m.person_key, m.content,
               CASE WHEN json_valid(t.metadata) THEN json_extract(t.metadata, '$.channel') END
                   AS channel,
               CASE WHEN json_valid(t.metadata)
                    THEN json_extract(t.metadata, '$.conversation_id') END AS conversation
        FROM memory_index m JOIN timeline t ON t.id = m.record_id
        WHERE m.rowid > ? AND t.event_type != '{MESSAGE_EVENT_TYPE}' ORDER BY m.rowid LIMIT ?
        """,
        (start[1], size),
    ).fetchall()
    history = conn.execute(
        f"""
        SELECT id, source_key, person_key, recorded_at, content
        FROM memory_history
        WHERE id > ? AND status = 'active' AND NOT ({is_message_source("source_key")})
        ORDER BY id LIMIT ?
        """,
        (start[0], size - len(timeline)),
    ).fetchall()
    if len(timeline) + len(history) < size:
        return None
    items = []
    for row in timeline:
        scope = (
            "timeline",
            row["visibility"],
            row["actor_type"],
            row["person_key"] or row["actor_id"],
            str(row["conversation"] or ""),
            str(row["channel"] or ""),
        )
        items.append(
            ConsolidationItem(
                f"timeline:{row['id']}",
                scope,
                row["content"],
                datetime.fromisoformat(row["timestamp"]).timestamp(),
            )
        )
    for row in history:
        instance, role, table, _ = json.loads(row["source_key"])
        items.append(
            ConsolidationItem(
                row["source_key"],
                ("history", instance, role, table, row["person_key"] or ""),
                row["content"],
                row["recorded_at"],
            )
        )
    end = (
        history[-1]["id"] if history else start[0],
        timeline[-1]["rowid"] if timeline else start[1],
    )
    return ConsolidationBatch(start, end, tuple(items))


def save_batch(
    conn: sqlite3.Connection,
    batch: ConsolidationBatch,
    extracted: Iterable[tuple[CandidateKind, str, ConsolidationItem]],
    now: float,
) -> bool:
    """原子更新游标和候选。两个工作者读到同一批时只接受一个。"""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if _cursor(conn) != batch.start:
            return False
        material = {item.reference: _source_is_memory(conn, item.reference) for item in batch.items}
        for item in batch.items:
            if material[item.reference] and (text := " ".join(item.text.split())):
                _merge(conn, "experience", text, item, now)
        for kind, text, item in extracted:
            if (
                kind != "experience"
                and material.get(item.reference)
                and (normalized := " ".join(text.split()))
            ):
                _merge(conn, kind, normalized, item, now)
        conn.execute(
            "UPDATE memory_consolidation_state SET history_cursor=?, timeline_cursor=? WHERE id=1",
            batch.end,
        )
        return True


def _source_is_memory(conn: sqlite3.Connection, reference: str) -> bool:
    prefix, separator, identifier = reference.partition(":")
    if prefix == "timeline" and separator:
        return (
            conn.execute(
                "SELECT 1 FROM timeline WHERE id = ? AND event_type != ?",
                (identifier, MESSAGE_EVENT_TYPE),
            ).fetchone()
            is not None
        )
    return (
        conn.execute(
            "SELECT 1 FROM memory_history WHERE source_key = ? "
            f"AND NOT ({is_message_source('source_key')})",
            (reference,),
        ).fetchone()
        is not None
    )


def _merge(
    conn: sqlite3.Connection,
    kind: CandidateKind,
    text: str,
    item: ConsolidationItem,
    now: float,
) -> None:
    fingerprint = sha256(json.dumps((kind, item.scope, text)).encode()).hexdigest()
    last_seen = item.timestamp if item.timestamp is not None else now
    conn.execute(
        """
        INSERT INTO memory_candidates VALUES (?, ?, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET last_seen = max(last_seen, excluded.last_seen)
        """,
        (fingerprint, kind, text, last_seen),
    )
    conn.execute(
        "INSERT OR IGNORE INTO memory_candidate_evidence VALUES (?, ?)",
        (fingerprint, item.reference),
    )


def read_candidates(
    conn: sqlite3.Connection, now: float, half_life_days: float, limit: int
) -> list[DerivedCandidate]:
    """读取最近的派生候选并按证据时间计算当前权重。"""
    if limit <= 0:
        return []
    conn.create_function("is_message_excerpt", 2, is_message_excerpt, deterministic=True)
    rows = conn.execute(
        f"""
        SELECT c.*, e.reference, h.content AS history_content
        FROM memory_candidates c
        JOIN memory_candidate_evidence e USING (fingerprint)
        LEFT JOIN memory_history h ON h.source_key = e.reference
        LEFT JOIN memory_edits history_edit
            ON history_edit.kind = 'history' AND history_edit.entry_id = h.id
        LEFT JOIN timeline t ON t.id = CAST(substr(e.reference, 10) AS INTEGER)
            AND e.reference = 'timeline:' || t.id
        LEFT JOIN memory_edits live_edit ON live_edit.kind = 'live' AND live_edit.entry_id = t.id
        WHERE (t.id IS NOT NULL AND coalesce(live_edit.is_enabled, 1)
               AND NOT coalesce(live_edit.is_removed, 0)
               AND live_edit.edited_content IS NULL
               AND (t.event_type != '{MESSAGE_EVENT_TYPE}' OR (
                   c.kind IN ('fact', 'preference') AND t.visibility = 'user_only'
                   AND t.actor_type = 'user' AND NOT is_message_excerpt(c.content, t.content))))
           OR (h.status = 'active' AND NOT ({is_message_source("h.source_key")})
               AND coalesce(history_edit.is_enabled, 1)
               AND NOT coalesce(history_edit.is_removed, 0)
               AND NOT coalesce(history_edit.is_bundle_removed, 0)
               AND history_edit.edited_content IS NULL
               AND (h.valid_from IS NULL OR h.valid_from <= ?)
               AND (h.valid_to IS NULL OR h.valid_to > ?))
        ORDER BY c.last_seen DESC, c.fingerprint, e.reference
        """,
        (now, now),
    )
    results: list[DerivedCandidate] = []
    for _, group in groupby(rows, key=lambda row: row["fingerprint"]):
        row = next(group)
        evidence = tuple(
            item["reference"]
            for item in chain((row,), group)
            if item["history_content"] is None
            or " ".join(item["history_content"].split()) == row["content"]
        )
        if not evidence:
            continue
        results.append(
            DerivedCandidate(
                row["kind"],
                row["content"],
                evidence,
                retention_weight(now, row["last_seen"], half_life_days),
            )
        )
        if len(results) == limit:
            break
    return results
