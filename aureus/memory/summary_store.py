"""长期总结的增量、同域游标与来源检查；只存派生内容及证据定位。"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection, Cursor, Row
from typing import Literal

from aureus.memory.history import HistoricalMemory, MemoryEvidence, MemorySource
from aureus.memory.summary_protocol import (
    PreviousFact,
    SummaryDelta,
    SummaryScope,
    SummaryWindow,
    validate_delta,
)

_REFERENCE_LIMIT = 16
type WindowCheck = Callable[[Connection, SummaryWindow], bool]
type EvidenceCheck = Callable[[Connection, SummaryScope, int, str, tuple[str, ...]], bool]
type ScopeCheck = Callable[[SummaryScope], bool]


@dataclass(frozen=True)
class SummaryHit:
    """仅返回经实时授权验证的事实，附证据定位而非消息正文。"""

    source: MemorySource
    recorded_at: datetime
    text: str
    evidence: tuple[MemoryEvidence, ...]
    relevance: float


def _summary_source(identifier: int) -> MemorySource:
    return MemorySource("aureus", "memory", "summary_facts", (identifier,))


def open_summaries(connection: Connection) -> None:
    """建立总结的范围游标、不可改写的批次和可被替代的事实。"""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS memory_summary_cursors (
            route TEXT NOT NULL, visibility TEXT NOT NULL, sequence INTEGER NOT NULL,
            PRIMARY KEY (route, visibility)
        );
        CREATE TABLE IF NOT EXISTS memory_summary_deltas (
            id INTEGER PRIMARY KEY, route TEXT NOT NULL, visibility TEXT NOT NULL,
            summary TEXT NOT NULL, entities TEXT NOT NULL, relations TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_summary_facts (
            id INTEGER PRIMARY KEY, delta_id INTEGER NOT NULL, local_id TEXT NOT NULL,
            text TEXT NOT NULL, predicate TEXT NOT NULL, value TEXT NOT NULL,
            sender_id TEXT NOT NULL, evidence TEXT NOT NULL, superseded_by INTEGER
        );
        CREATE INDEX IF NOT EXISTS memory_summary_delta_scope
            ON memory_summary_deltas (route, visibility, id);
        CREATE INDEX IF NOT EXISTS memory_summary_fact_delta
            ON memory_summary_facts (delta_id, id);
    """)


def summary_cursor(connection: Connection, scope: SummaryScope) -> int:
    """读取完整来源范围的专用游标，不使用时间线或旧整理的游标。"""
    row = connection.execute(
        "SELECT sequence FROM memory_summary_cursors WHERE route=? AND visibility=?", scope
    ).fetchone()
    return row["sequence"] if row is not None else 0


def _locations(row: Row) -> list[tuple[int, str, list[str]]]:
    return json.loads(row["evidence"])


def _source_exists(
    connection: Connection, scope: SummaryScope, row: Row, check: EvidenceCheck
) -> bool:
    locations = _locations(row)
    return bool(locations) and all(
        check(connection, scope, sequence, message_id, tuple(paths))
        for sequence, message_id, paths in locations
    )


def _evidence(scope: SummaryScope, row: Row) -> tuple[MemoryEvidence, ...]:
    evidence: list[MemoryEvidence] = []
    for sequence, identifier, paths in _locations(row):
        table = "timeline" if scope[0].startswith("web:") else "inbox_messages"
        location = MemorySource("aureus", "platform", table, (*scope, sequence, identifier))
        evidence.append(MemoryEvidence("platform_message", identifier, location))
        evidence.extend(MemoryEvidence("platform_image", path, location) for path in paths)
    return tuple(evidence)


def _scope(row: Row) -> SummaryScope:
    return row["route"], row["visibility"]


class SummaryVisibilityChangedError(RuntimeError):
    """管理查询期间授权变化，不能披露本次统计。"""


def authorized_summary_scopes(connection: Connection, allows: ScopeCheck) -> set[SummaryScope]:
    """从同一数据库快照复核目前管理或召回可见的完整来源范围。"""
    rows = connection.execute("SELECT DISTINCT route, visibility FROM memory_summary_deltas")
    return {
        scope for row in rows if allows(scope := _scope(row))
    }


_RESOURCE_ROWS = """SELECT fact.*, delta.route, delta.visibility, delta.recorded_at,
    edit.edited_content, coalesce(edit.is_enabled, 1) AS is_enabled,
    coalesce(edit.is_removed, 0) AS is_removed
    FROM memory_summary_facts fact JOIN memory_summary_deltas delta ON delta.id=fact.delta_id
    LEFT JOIN memory_edits edit ON edit.kind='summary' AND edit.entry_id=fact.id"""


def summary_catalog(
    connection: Connection, scopes: set[SummaryScope], check: EvidenceCheck
) -> list[tuple[int, str]]:
    """列出当前管理者可知的键与状态；授权范围外不返回键或计数。"""
    return [
        (row["id"], _resource_status(connection, row, check))
        for row in connection.execute(_RESOURCE_ROWS)
        if _scope(row) in scopes
    ]


def _resource_status(
    connection: Connection, row: Row, check: EvidenceCheck
) -> Literal["active", "missing", "superseded"]:
    if row["superseded_by"] is not None:
        return "superseded"
    return "active" if _source_exists(connection, _scope(row), row, check) else "missing"


def summary_detail(
    connection: Connection, source: MemorySource, allow_scope: ScopeCheck, check: EvidenceCheck
) -> HistoricalMemory | None:
    """只返回对当前管理者可知的派生值，绝不返回平台证据正文。"""
    if (
        len(source.key) != 1 or type(source.key[0]) is not int or not 0 < source.key[0] < 2**63
        or source != _summary_source(source.key[0])
    ):
        return None
    row = connection.execute(_RESOURCE_ROWS + " WHERE fact.id=?", source.key).fetchone()
    if row is None or not allow_scope(_scope(row)):
        return None
    status = _resource_status(connection, row, check)
    if not allow_scope(_scope(row)):
        return None
    evidence = _evidence(_scope(row), row) if status == "active" else ()
    return HistoricalMemory(
        source,
        (row["edited_content"] or row["text"]) if status == "active" else "",
        "会话事实",
        recorded_at=datetime.fromisoformat(row["recorded_at"]),
        status=status,
        evidence=evidence,
        entry_id=row["id"],
        is_enabled=bool(row["is_enabled"]),
        is_recallable=status == "active" and bool(row["is_enabled"]) and not row["is_removed"],
        is_removed=bool(row["is_removed"]),
    )


def _facts(connection: Connection, scope: SummaryScope, limit: int = -1) -> Cursor:
    return connection.execute(
        _RESOURCE_ROWS + """ WHERE delta.route=? AND delta.visibility=?
        AND fact.superseded_by IS NULL ORDER BY fact.id DESC LIMIT ?""",
        (*scope, limit),
    )


def previous_summary(
    connection: Connection, scope: SummaryScope, check: EvidenceCheck
) -> tuple[tuple[str, ...], tuple[PreviousFact, ...]]:
    """取最近被采纳的有效摘要；失去证据或包含旧结论的整条摘要不重读。"""
    deltas = connection.execute(
        """SELECT delta.id, delta.summary FROM memory_summary_deltas delta
        WHERE delta.route=? AND delta.visibility=? AND NOT EXISTS (
            SELECT 1 FROM memory_summary_facts fact
            LEFT JOIN memory_edits edit ON edit.kind='summary' AND edit.entry_id=fact.id
            WHERE fact.delta_id=delta.id AND (fact.superseded_by IS NOT NULL
                OR NOT coalesce(edit.is_enabled, 1) OR coalesce(edit.is_removed, 0)
                OR edit.edited_content IS NOT NULL)
        ) ORDER BY delta.id DESC LIMIT ?""",
        (*scope, _REFERENCE_LIMIT),
    ).fetchall()
    summaries: list[str] = []
    for delta in reversed(deltas):
        evidence = connection.execute(
            "SELECT * FROM memory_summary_facts WHERE delta_id=?", (delta["id"],)
        )
        if all(_source_exists(connection, scope, row, check) for row in evidence):
            summaries.append(delta["summary"])
    facts = [
        PreviousFact(row["id"], row["text"], row["sender_id"], row["predicate"], row["value"])
        for row in _facts(connection, scope, _REFERENCE_LIMIT)
        if row["is_enabled"] and not row["is_removed"] and row["edited_content"] is None
        and _source_exists(connection, scope, row, check)
    ]
    return tuple(summaries), tuple(facts)


def _write_delta(
    connection: Connection, window: SummaryWindow, delta: SummaryDelta, timestamp: datetime
) -> None:
    material = {item.message_id: item for item in window.messages}
    delta_id = connection.execute(
        """INSERT INTO memory_summary_deltas (route, visibility, summary, entities, relations,
        recorded_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            *window.scope,
            delta.summary,
            json.dumps(delta.entities, ensure_ascii=False),
            json.dumps([relation.model_dump() for relation in delta.relations], ensure_ascii=False),
            timestamp.isoformat(),
        ),
    ).fetchone()["id"]
    for fact in delta.facts:
        locations = [
            (
                material[item.message_id].sequence,
                item.message_id,
                [
                    image.component_path
                    for image in fact.image_evidence
                    if image.message_id == item.message_id
                ],
            )
            for item in (fact.source, fact.context)
            if item is not None
        ]
        identifier = connection.execute(
            """INSERT INTO memory_summary_facts (delta_id, local_id, text, predicate, value,
            sender_id, evidence)
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (
                delta_id,
                fact.fact_id,
                fact.text,
                fact.predicate,
                fact.value,
                fact.claim_owner,
                json.dumps(locations, ensure_ascii=False),
            ),
        ).fetchone()["id"]
        if fact.replaces is not None:
            connection.execute(
                "UPDATE memory_summary_facts SET superseded_by=? WHERE id=?",
                (identifier, fact.replaces),
            )


def save_summary(
    connection: Connection,
    window: SummaryWindow,
    delta: SummaryDelta,
    timestamp: datetime,
    check_window: WindowCheck,
    check_evidence: EvidenceCheck,
) -> bool:
    """游标比较、来源重验、去重和整批新事实共用一个写事务。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if (
            not window.messages
            or summary_cursor(connection, window.scope) != window.start
            or not check_window(connection, window)
        ):
            return False
        summary, previous_facts = previous_summary(connection, window.scope, check_evidence)
        if (summary, previous_facts) != (window.previous_summaries, window.previous_facts):
            return False
        validate_delta(delta, window)
        if delta.facts:
            proposals = {(fact.claim_owner, fact.predicate, fact.value) for fact in delta.facts}
            for row in _facts(connection, window.scope):
                if (
                    row["sender_id"],
                    row["predicate"],
                    row["value"],
                ) in proposals and _source_exists(connection, window.scope, row, check_evidence):
                    return False
            _write_delta(connection, window, delta, timestamp)
        connection.execute(
            """INSERT INTO memory_summary_cursors VALUES (?, ?, ?)
            ON CONFLICT(route, visibility) DO UPDATE SET sequence=excluded.sequence""",
            (*window.scope, window.messages[-1].sequence),
        )
        return True


def search_summaries(
    connection: Connection,
    scope: SummaryScope,
    keywords: Sequence[str],
    check: EvidenceCheck,
    limit: int,
    *,
    since: datetime | None,
    before: datetime | None,
) -> list[SummaryHit]:
    """只召回本范围仍有真实来源的事实，不召回自由叙述或旧值。"""
    terms = {word.strip().casefold() for word in keywords if word.strip()}
    if not terms:
        return []
    hits = []
    for row in _facts(connection, scope):
        text = row["edited_content"] or row["text"]
        relevance = sum(word in text.casefold() for word in terms)
        timestamp = datetime.fromisoformat(row["recorded_at"])
        if (
            not relevance
            or not row["is_enabled"]
            or row["is_removed"]
            or (since is not None and timestamp < since)
            or (before is not None and timestamp >= before)
            or not _source_exists(connection, scope, row, check)
        ):
            continue
        hits.append(
            SummaryHit(_summary_source(row["id"]), timestamp, text, _evidence(scope, row),
                       relevance)
        )
    hits.sort(key=lambda hit: (hit.relevance, hit.recorded_at), reverse=True)
    return hits[:limit]


def search_authorized_summaries(
    connection: Connection,
    keywords: Sequence[str],
    check: EvidenceCheck,
    allow_scope: ScopeCheck,
    limit: int,
    *,
    since: datetime | None,
    before: datetime | None,
) -> list[SummaryHit]:
    """跨会话读取获准的派生事实，不开放平台原文。"""
    scopes = authorized_summary_scopes(connection, allow_scope)
    hits: list[SummaryHit] = []
    for scope in scopes:
        if allow_scope(scope):
            hits.extend(
                search_summaries(
                    connection,
                    scope,
                    keywords,
                    check,
                    limit,
                    since=since,
                    before=before,
                )
            )
    current = authorized_summary_scopes(connection, allow_scope)
    if scopes != current:
        return []
    hits.sort(key=lambda hit: (hit.relevance, hit.recorded_at, hit.source.key), reverse=True)
    return hits[:limit]
