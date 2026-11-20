"""记忆全文索引：只管增量保存和检索，内容由调用方决定（D80）。"""

import sqlite3
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureus.common.sqlite import open_connection
from aureus.timeline import TimelineDatabase, TimelineQuery, TimelineRecord

# D80 第 4 条：单条记忆交给回复器时的正文上限。
MAX_HIT_CHARS = 180


@dataclass(frozen=True)
class MemoryEntry:
    """心跳决定要收进索引的正文和称呼。"""

    actor_label: str
    text: str


@dataclass(frozen=True)
class MemoryHit:
    """带来源和时刻的检索结果；正文已按 D80 截短。"""

    record_id: int
    recorded_at: datetime
    actor_label: str
    text: str


class MemoryIndex:
    """可重建的 FTS5 trigram 索引；不解释事件、不改时间线。"""

    def __init__(self, db_path: Path, timeline: TimelineDatabase) -> None:
        self._db_path, self._timeline = db_path, timeline
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """创建派生表；SQLite 不支持 trigram 时抛 sqlite3.Error。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(
                    content, actor_label UNINDEXED, record_id UNINDEXED,
                    recorded_at UNINDEXED, visibility UNINDEXED, tokenize = 'trigram'
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
                        "(content, actor_label, record_id, recorded_at, visibility) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            entry.text,
                            entry.actor_label,
                            record.id,
                            record.timestamp.isoformat(),
                            record.visibility.value,
                        ),
                    )
            conn.execute(
                "UPDATE memory_index_state SET indexed_through = ? WHERE id = 1",
                (records[-1].id,),
            )
        return len(records)

    def search(
        self, keywords: Sequence[str], exclude_ids: Collection[int], limit: int
    ) -> list[MemoryHit]:
        """多词取或，排除当前对话，最新在前；每次自开连接供后台线程调用。"""
        clauses: list[str] = []
        parameters: list[str | int] = []
        for keyword in keywords:
            if len(keyword) >= 3:
                # 用子查询组合 MATCH 与 LIKE，避免 FTS5 在 OR 表达式里拒绝 MATCH。
                clauses.append(
                    "rowid IN (SELECT rowid FROM memory_index WHERE memory_index MATCH ?)"
                )
                parameters.append('"' + keyword.replace('"', '""') + '"')
            elif len(keyword) == 2:
                clauses.append("content LIKE ? ESCAPE '!'")
                escaped = keyword.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                parameters.append(f"%{escaped}%")
        if not clauses:
            return []
        where = "(" + " OR ".join(clauses) + ")"
        if exclude_ids:
            where += " AND record_id NOT IN (" + ",".join("?" for _ in exclude_ids) + ")"
            parameters.extend(exclude_ids)
        parameters.append(limit)
        conn = open_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT record_id, recorded_at, actor_label, content FROM memory_index WHERE "
                + where
                + " ORDER BY CAST(record_id AS INTEGER) DESC LIMIT ?",
                parameters,
            ).fetchall()
            return [
                MemoryHit(
                    int(r["record_id"]),
                    datetime.fromisoformat(r["recorded_at"]),
                    r["actor_label"],
                    r["content"][:MAX_HIT_CHARS],
                )
                for r in rows
            ]
        finally:
            conn.close()
