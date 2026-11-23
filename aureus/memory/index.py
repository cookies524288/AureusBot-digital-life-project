"""记忆全文索引：只管增量保存和检索，内容由调用方决定（D80）。"""

import sqlite3
from collections.abc import Callable, Collection, Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureus.common import Clock, MemoryConfig, SystemClock
from aureus.common.sqlite import open_connection
from aureus.memory.consolidation import (
    CandidateKind,
    ConsolidationBatch,
    ConsolidationItem,
    DerivedCandidate,
    open_consolidation,
    read_batch,
    read_candidates,
    retention_weight,
    save_batch,
)
from aureus.memory.history import (
    HistoricalMemory,
    MemoryEvidence,
    MemorySource,
    open_history,
    publish_history,
    read_history,
    search_history,
)
from aureus.memory.sparse import keyword_conditions, ranked_matches, relevance_expression
from aureus.timeline import TimelineDatabase, TimelineQuery, TimelineRecord

# D80 第 4 条：单条记忆交给回复器时的正文上限。
MAX_HIT_CHARS = 180
_LIVE_INDEX_SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(
        content, actor_label UNINDEXED, record_id UNINDEXED,
        recorded_at UNINDEXED, visibility UNINDEXED, person_key UNINDEXED,
        tokenize = 'trigram'
    )
"""


def _open_live_index(conn: sqlite3.Connection) -> None:
    """让全文条目的行号等于时间线序号，整理游标可跨重建使用。"""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_index)")}
        state_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(memory_index_state)")
        }
        if "person_key" not in columns or not state_columns:
            # 派生表从时间线重建，已提交的整理游标随原件保留。
            conn.execute("DROP TABLE IF EXISTS memory_index")
            conn.execute("DROP TABLE IF EXISTS memory_index_state")
            conn.execute(_LIVE_INDEX_SCHEMA)
            conn.execute("""
                CREATE TABLE memory_index_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1), indexed_through INTEGER NOT NULL,
                    stable_rowid INTEGER NOT NULL DEFAULT 1
                )
            """)
            conn.execute("INSERT INTO memory_index_state(id, indexed_through) VALUES (1, 0)")
        elif "stable_rowid" not in state_columns:
            conn.execute("ALTER TABLE memory_index RENAME TO memory_index_previous")
            conn.execute(_LIVE_INDEX_SCHEMA)
            conn.execute("""
                INSERT INTO memory_index(rowid, content, actor_label, record_id,
                                         recorded_at, visibility, person_key)
                SELECT record_id, content, actor_label, record_id,
                       recorded_at, visibility, person_key FROM memory_index_previous
            """)
            conn.execute("DROP TABLE memory_index_previous")
            conn.execute(
                "ALTER TABLE memory_index_state ADD COLUMN stable_rowid INTEGER NOT NULL DEFAULT 1"
            )


@dataclass(frozen=True)
class MemoryEntry:
    """心跳决定要收进索引的正文和称呼。"""

    actor_label: str
    text: str
    person_key: str | None = None


@dataclass(frozen=True)
class MemoryHit:
    """带来源和时刻的检索结果；正文已按 D80 截短。"""

    record_id: int | None
    recorded_at: datetime | None
    actor_label: str
    text: str
    source: MemorySource | None = None
    time_precision: str | None = None
    evidence: tuple[MemoryEvidence, ...] = ()
    relevance: float = 0
    sparse_score: float = 0.0


class MemoryIndex:
    """可重建的 FTS5 trigram 索引；不解释事件、不改时间线。"""

    def __init__(
        self,
        db_path: Path,
        timeline: TimelineDatabase,
        clock: Clock | None = None,
        *,
        config: MemoryConfig | None = None,
    ) -> None:
        self._db_path, self._timeline = db_path, timeline
        self._clock = clock if clock is not None else SystemClock()
        self._config = config or MemoryConfig()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """创建派生表；SQLite 不支持 trigram 时抛 sqlite3.Error。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            open_history(conn)
            _open_live_index(conn)
            open_consolidation(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        """关闭增量索引使用的连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def index_more(
        self, limit: int, describe: Callable[[TimelineRecord], MemoryEntry | None]
    ) -> int:
        """最多看 limit 条；正文与进度同批提交，异常回滚并往外抛。"""
        conn = self._conn
        if conn is None:
            raise RuntimeError("记忆索引还没有打开")
        through = conn.execute("SELECT indexed_through FROM memory_index_state WHERE id = 1")
        records = self._timeline.query(TimelineQuery(since_id=through.fetchone()[0], limit=limit))
        if not records:
            return 0
        with conn:
            for record in records:
                entry = describe(record)
                if entry is not None:
                    conn.execute(
                        "INSERT INTO memory_index "
                        "(rowid, content, actor_label, record_id, recorded_at, "
                        "visibility, person_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.id,
                            entry.text,
                            entry.actor_label,
                            record.id,
                            record.timestamp.isoformat(),
                            record.visibility.value,
                            entry.person_key,
                        ),
                    )
            conn.execute(
                "UPDATE memory_index_state SET indexed_through = ? WHERE id = 1",
                (records[-1].id,),
            )
        return len(records)

    def publish(
        self, records: Iterable[HistoricalMemory], *, snapshot: tuple[str, str] | None = None
    ) -> int:
        """写入长期记忆原件及派生索引，保留归档记录。"""
        if self._conn is None:
            raise RuntimeError("记忆索引还没有打开")
        return publish_history(self._conn, records, snapshot)

    def get_history(self, source: MemorySource) -> HistoricalMemory | None:
        """读指定来源的原文及证据，包括归档记录。"""
        if self._conn is None:
            raise RuntimeError("记忆索引还没有打开")
        return read_history(self._conn, source)

    def consolidation_batch(self, size: int) -> ConsolidationBatch | None:
        """在后台连接中查是否累积满一批待整理的原件。"""
        with closing(open_connection(self._db_path)) as conn:
            return read_batch(conn, size)

    def save_consolidation(
        self,
        batch: ConsolidationBatch,
        extracted: Iterable[tuple[CandidateKind, str, ConsolidationItem]],
    ) -> bool:
        """在后台连接中原子提交整理结果。"""
        with closing(open_connection(self._db_path)) as conn:
            return save_batch(conn, batch, extracted, self._clock.now().timestamp())

    def consolidated_candidates(self, limit: int) -> list[DerivedCandidate]:
        """读取带原件指针的派生候选及当前权重。"""
        with closing(open_connection(self._db_path)) as conn:
            return read_candidates(
                conn,
                self._clock.now().timestamp(),
                self._config.consolidation_half_life_days,
                limit,
            )

    def search(
        self,
        keywords: Sequence[str],
        exclude_ids: Collection[int],
        limit: int,
        *,
        person_key: str | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
        include_user_extractions: bool = True,
    ) -> list[MemoryHit]:
        """在同一次查询内召回历史与新经历，按稳定人物键和时间过滤。"""
        clauses, terms = keyword_conditions(keywords, "memory_index", "rowid")
        if limit <= 0 or (
            not clauses and (keywords or (person_key is None and since is None and before is None))
        ):
            return []
        parameters: list[str | int | float] = list(terms)
        where = ["(" + " OR ".join(clauses) + ")"] if clauses else []
        if person_key is not None:
            where.append("person_key = ?")
            parameters.append(person_key)
        if since is not None:
            where.append("julianday(recorded_at) >= julianday(?)")
            parameters.append(since.isoformat())
        if before is not None:
            where.append("julianday(recorded_at) < julianday(?)")
            parameters.append(before.isoformat())
        if exclude_ids:
            where.append("record_id NOT IN (" + ",".join("?" for _ in exclude_ids) + ")")
            parameters.extend(exclude_ids)
        evidence_where = where[1:] if clauses else where
        evidence_parameters = parameters[len(terms) :]
        parameters.append(limit)
        rank_prefix, score, rank_parameters = ranked_matches(keywords, "memory_index")
        conn = open_connection(self._db_path)
        try:
            conn.execute("BEGIN")
            now = self._clock.now()
            rows = conn.execute(
                rank_prefix
                + "SELECT record_id, recorded_at, actor_label, content, "
                + relevance_expression(clauses)
                + f" AS relevance, {score.format(row_id='memory_index.rowid')} AS sparse_score "
                + "FROM memory_index WHERE "
                + " AND ".join(where)
                + " ORDER BY relevance DESC, recorded_at DESC, sparse_score, "
                + "CAST(record_id AS INTEGER) DESC LIMIT ?",
                [*rank_parameters, *terms, *parameters],
            ).fetchall()
            if (
                clauses
                and include_user_extractions
                and self._config.consolidation_enabled
                and self._config.consolidation_extract_enabled
            ):
                aliases, alias_terms = keyword_conditions(keywords, "memory_extracted_fts", "rowid")
                conn.create_function("retention_weight", 3, retention_weight, deterministic=True)
                # 派生词只扩大匹配入口，返回的编号、内容、人物和时间全部取自证据。
                # 先在有效原件中取每个来源的最高分，再与直接命中共同裁剪。
                rows.extend(
                    conn.execute(
                        "WITH matches AS (SELECT rowid, "
                        + relevance_expression(aliases)
                        + " AS words FROM memory_extracted_fts WHERE "
                        + " OR ".join(aliases)
                        + ") "
                        + "SELECT m.record_id, m.recorded_at, m.actor_label, m.content, "
                        + "max(words * retention_weight(?, c.last_seen, ?)) AS relevance, "
                        + "0.0 AS sparse_score FROM matches JOIN memory_candidates c "
                        + "ON c.rowid = matches.rowid JOIN memory_candidate_evidence e "
                        + "ON e.fingerprint = c.fingerprint JOIN memory_index m "
                        + "ON m.rowid = CAST(substr(e.reference, 10) AS INTEGER) "
                        + "JOIN timeline t ON t.id = m.rowid AND t.content = m.content "
                        + "WHERE e.reference = 'timeline:' || m.rowid AND t.actor_type='user' "
                        + "AND t.visibility='user_only' AND t.event_type='message' "
                        + "AND c.kind IN ('fact', 'preference') "
                        + "".join(" AND " + clause for clause in evidence_where)
                        + " GROUP BY m.rowid ORDER BY relevance DESC, "
                        "m.recorded_at DESC, m.rowid DESC LIMIT ?",
                        [
                            *alias_terms,
                            *alias_terms,
                            now.timestamp(),
                            self._config.consolidation_half_life_days,
                            *evidence_parameters,
                            limit,
                        ],
                    ).fetchall()
                )
                rows.sort(
                    key=lambda r: (
                        r["relevance"],
                        r["recorded_at"],
                        -r["sparse_score"],
                        int(r["record_id"]),
                    ),
                    reverse=True,
                )
                # 同一原件可由多个词、建议及原文命中，只占一个位置。
                distinct = {}
                for row in rows:
                    distinct.setdefault(int(row["record_id"]), row)
                rows = list(distinct.values())[:limit]
            hits = [
                MemoryHit(
                    int(r["record_id"]),
                    datetime.fromisoformat(r["recorded_at"]),
                    r["actor_label"],
                    r["content"][:MAX_HIT_CHARS],
                    relevance=r["relevance"],
                    sparse_score=r["sparse_score"],
                )
                for r in rows
            ]
            history = search_history(
                conn,
                keywords,
                limit,
                now=now,
                person_key=person_key,
                since=since,
                before=before,
            )
        finally:
            conn.close()
        if not history:
            return hits
        hits.extend(
            MemoryHit(
                None,
                record.recorded_at,
                record.actor_label,
                record.text[:MAX_HIT_CHARS],
                record.source,
                record.time_precision,
                record.evidence,
                score,
                sparse_score,
            )
            for record, score, sparse_score in history
        )
        hits.sort(
            key=lambda hit: (
                hit.relevance,
                hit.recorded_at.timestamp() if hit.recorded_at else float("-inf"),
            ),
            reverse=True,
        )
        return hits[:limit]
