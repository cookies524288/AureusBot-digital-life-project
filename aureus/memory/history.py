"""迁入的长期记忆原件、溯源和可重建全文检索。

当前事实的状态与时间判定移植自安装版 A_Memorix 2.0.0
core/storage/metadata_fact.py::list_current_person_fact_claims（AGPL-3.0）。
适配为通用记忆状态和时钟参数，保留稳定性等源字段供发布层解释。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from aureus.memory.sparse import keyword_conditions, ranked_matches, relevance_expression

type SourceValue = str | int | float | dict[str, str]
type MemoryStatus = Literal[
    "active", "inactive", "conflicted", "superseded", "retracted", "deleted", "missing"
]


@dataclass(frozen=True)
class MemorySource:
    """3.2 的实例、库角色、表及有序原主键。"""

    instance: str
    role: str
    table: str
    key: tuple[SourceValue, ...]

    def encode(self) -> str:
        """为 SQLite 保留来源范围和主键类型。"""
        return json.dumps(
            [self.instance, self.role, self.table, self.key],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def decode(cls, value: str) -> MemorySource:
        """读回由 encode 写入的来源。"""
        instance, role, table, key = json.loads(value)
        return cls(instance, role, table, tuple(key))


@dataclass(frozen=True)
class MemoryEvidence:
    """证据或关系指向的原始标识，不能解析的引用同样保留。"""

    kind: str
    reference: str
    source: MemorySource | None = None
    status: MemoryStatus | None = None


@dataclass(frozen=True)
class HistoricalMemory:
    """迁入的一件资料，标签仅供展示，人物键用于精确过滤。"""

    source: MemorySource
    text: str
    actor_label: str = ""
    person_key: str | None = None
    recorded_at: datetime | None = None
    time_precision: str | None = None
    status: MemoryStatus = "active"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    evidence: tuple[MemoryEvidence, ...] = ()
    received_at: datetime | None = None


class MemorySnapshotConflictError(ValueError):
    """已发布的实例快照与本次来源或时间规则不同。"""


def _epoch(value: datetime | None) -> float | None:
    return value.astimezone(UTC).timestamp() if value is not None else None


def _date(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value is not None else None


def _decode(row: sqlite3.Row) -> HistoricalMemory:
    evidence: list[MemoryEvidence] = []
    for item in json.loads(row["evidence"]):
        source = item["source"]
        if source is not None:
            source["key"] = tuple(source["key"])
        evidence.append(
            MemoryEvidence(
                item["kind"],
                item["reference"],
                MemorySource(**source) if source else None,
                item.get("status"),
            )
        )
    return HistoricalMemory(
        MemorySource.decode(row["source_key"]),
        row["content"],
        row["actor_label"],
        row["person_key"],
        _date(row["recorded_at"]),
        row["time_precision"],
        row["status"],
        _date(row["valid_from"]),
        _date(row["valid_to"]),
        tuple(evidence),
        _date(row["received_at"]),
    )


def open_history(conn: sqlite3.Connection) -> None:
    """打开原件表，派生索引缺失时从原件重建。"""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_history (
                id INTEGER PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                actor_label TEXT NOT NULL,
                person_key TEXT,
                recorded_at REAL,
                time_precision TEXT,
                status TEXT NOT NULL,
                valid_from REAL,
                valid_to REAL,
                evidence TEXT NOT NULL,
                received_at REAL
            )
        """)
        columns = conn.execute("PRAGMA table_info(memory_history)")
        if not any(column[1] == "received_at" for column in columns):
            conn.execute("ALTER TABLE memory_history ADD COLUMN received_at REAL")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS memory_history_person_time
            ON memory_history(person_key, recorded_at)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_history_snapshots (
                instance TEXT PRIMARY KEY, fingerprint TEXT NOT NULL
            )
        """)
        if (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_history_fts'").fetchone()
            is None
        ):
            conn.execute("""
                CREATE VIRTUAL TABLE memory_history_fts USING fts5(
                    content, content = 'memory_history', content_rowid = 'id', tokenize = 'trigram'
                )
            """)
            conn.execute("INSERT INTO memory_history_fts(memory_history_fts) VALUES ('rebuild')")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_history_insert AFTER INSERT ON memory_history BEGIN
                INSERT INTO memory_history_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_history_update AFTER UPDATE ON memory_history BEGIN
                INSERT INTO memory_history_fts(memory_history_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                INSERT INTO memory_history_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)


def publish_history(
    conn: sqlite3.Connection,
    records: Iterable[HistoricalMemory],
    snapshot: tuple[str, str] | None,
) -> int:
    """按来源原子发布一批，返回首次发布或更新的条数。"""
    written = 0
    with conn:
        if snapshot is not None:
            conn.execute("INSERT OR IGNORE INTO memory_history_snapshots VALUES (?, ?)", snapshot)
            match = conn.execute(
                "SELECT fingerprint FROM memory_history_snapshots WHERE instance = ?", snapshot[:1]
            ).fetchone()
            if match[0] != snapshot[1]:
                raise MemorySnapshotConflictError
        for record in records:
            result = conn.execute(
                """
                INSERT INTO memory_history (
                    source_key, content, actor_label, person_key, recorded_at, time_precision,
                    status, valid_from, valid_to, evidence, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    (content, actor_label, person_key, recorded_at, time_precision,
                     status, valid_from, valid_to, evidence, received_at) =
                    (excluded.content, excluded.actor_label, excluded.person_key,
                     excluded.recorded_at, excluded.time_precision, excluded.status,
                     excluded.valid_from, excluded.valid_to, excluded.evidence,
                     excluded.received_at)
                WHERE (content, actor_label, person_key, recorded_at, time_precision,
                       status, valid_from, valid_to, evidence, received_at) IS NOT
                      (excluded.content, excluded.actor_label, excluded.person_key,
                       excluded.recorded_at, excluded.time_precision, excluded.status,
                       excluded.valid_from, excluded.valid_to, excluded.evidence,
                       excluded.received_at)
                """,
                (
                    record.source.encode(),
                    record.text,
                    record.actor_label,
                    record.person_key,
                    _epoch(record.recorded_at),
                    record.time_precision,
                    record.status,
                    _epoch(record.valid_from),
                    _epoch(record.valid_to),
                    json.dumps([asdict(item) for item in record.evidence], ensure_ascii=False),
                    _epoch(record.received_at),
                ),
            )
            written += result.rowcount
    return written


def read_history(conn: sqlite3.Connection, source: MemorySource) -> HistoricalMemory | None:
    """按来源读原件，包含已归档状态。"""
    row = conn.execute(
        "SELECT * FROM memory_history WHERE source_key = ?", (source.encode(),)
    ).fetchone()
    return _decode(row) if row is not None else None


def browse_history(
    conn: sqlite3.Connection, query: str, status: str, offset: int, limit: int
) -> list[HistoricalMemory]:
    """返回一页原件，包括被归档的记录。"""
    rows = conn.execute(
        """SELECT * FROM memory_history
           WHERE (? = '' OR status = ?)
             AND (? = '' OR instr(lower(content), lower(?)) > 0
                         OR instr(lower(actor_label), lower(?)) > 0
                         OR instr(lower(source_key), lower(?)) > 0)
           ORDER BY id DESC LIMIT ? OFFSET ?""",
        (status, status, query, query, query, query, limit, offset),
    ).fetchall()
    return [_decode(row) for row in rows]


def search_history(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    limit: int,
    *,
    now: datetime,
    person_keys: tuple[str, ...] | None,
    since: datetime | None,
    before: datetime | None,
) -> list[tuple[HistoricalMemory, int, float]]:
    """只查当前有效的资料。起始时间包含端点，截止时间排除端点。"""
    clauses, terms = keyword_conditions(keywords, "memory_history_fts", "id")
    if not clauses and person_keys is None and since is None and before is None:
        return []
    # 上游的有效区间为 [valid_from, valid_to)，查询时刻来自 Aureus 时钟。
    where = [
        "status = 'active'",
        "(valid_from IS NULL OR valid_from <= ?)",
        "(valid_to IS NULL OR valid_to > ?)",
    ]
    point = now.timestamp()
    parameters: list[str | int | float] = [point, point]
    if clauses:
        where.append("(" + " OR ".join(clauses) + ")")
        parameters.extend(terms)
    elif keywords:
        return []
    _append_filters(where, parameters, person_keys, since, before)
    parameters.append(limit)
    rank_prefix, score, rank_parameters = ranked_matches(keywords, "memory_history_fts")
    rows = conn.execute(
        rank_prefix
        + "SELECT *, "
        + relevance_expression(clauses)
        + f" AS relevance, {score.format(row_id='memory_history.id')} AS sparse_score "
        + "FROM memory_history WHERE "
        + " AND ".join(where)
        + " ORDER BY relevance DESC, recorded_at DESC, sparse_score, source_key LIMIT ?",
        [*rank_parameters, *terms, *parameters],
    )
    return [(_decode(row), row["relevance"], row["sparse_score"]) for row in rows]


def _append_filters(
    where: list[str],
    parameters: list[str | int | float],
    person_keys: tuple[str, ...] | None,
    since: datetime | None,
    before: datetime | None,
) -> None:
    if person_keys is not None:
        where.append("person_key IN (" + ",".join("?" for _ in person_keys) + ")")
        parameters.extend(person_keys)
    if since is not None:
        where.append("recorded_at >= ?")
        parameters.append(since.timestamp())
    if before is not None:
        where.append("recorded_at < ?")
        parameters.append(before.timestamp())
