"""记忆全文索引：只管增量保存和检索，内容由调用方决定（D80）。"""

import sqlite3
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureus.common import Clock, SystemClock
from aureus.common.sqlite import open_connection
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
    relevance: int = 0
    sparse_score: float = 0.0


class MemoryIndex:
    """可重建的 FTS5 trigram 索引；不解释事件、不改时间线。"""

    def __init__(
        self, db_path: Path, timeline: TimelineDatabase, clock: Clock | None = None
    ) -> None:
        self._db_path, self._timeline = db_path, timeline
        self._clock = clock if clock is not None else SystemClock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """创建派生表；SQLite 不支持 trigram 时抛 sqlite3.Error。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            open_history(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_index)")}
            if "person_key" not in columns:
                # D80 派生内容从追加时间线回填，旧表或缺表均从头开始。
                conn.executescript("""
                    DROP TABLE IF EXISTS memory_index;
                    DROP TABLE IF EXISTS memory_index_state;
                """)
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(
                    content, actor_label UNINDEXED, record_id UNINDEXED,
                    recorded_at UNINDEXED, visibility UNINDEXED, person_key UNINDEXED,
                    tokenize = 'trigram'
                );
                CREATE TABLE IF NOT EXISTS memory_index_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1), indexed_through INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO memory_index_state VALUES (1, 0);
            """)
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
                        "(content, actor_label, record_id, recorded_at, visibility, person_key) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
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

    def search(
        self,
        keywords: Sequence[str],
        exclude_ids: Collection[int],
        limit: int,
        *,
        person_key: str | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
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
        parameters.append(limit)
        rank_prefix, score, rank_parameters = ranked_matches(keywords, "memory_index")
        conn = open_connection(self._db_path)
        try:
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
                now=self._clock.now(),
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
