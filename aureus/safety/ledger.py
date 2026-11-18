"""账本 ledger：记录每一次真正发出去的模型请求（D37、D39，06 号文档 12.2 节）。

表 `ledger` 只属于安全子系统，放在 aureus.db 里。只追加：数据库触发器禁止修改和删除，
审计记录不能删（06 号文档第 7 节）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aureus.common import Clock, SystemClock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import open_connection
from aureus.organs import CallAttempt

# 时间固定写成带微秒的 UTC，格式等宽，这样 SQLite 里按字符串比较大小就等于按时间比较。
_TIMESTAMP_PRECISION = "microseconds"

_DECEMBER = 12


@dataclass(frozen=True)
class LedgerEntry:
    """账本里的一行，也就是一次模型请求。"""

    id: int
    timestamp: datetime
    organ_id: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    estimated: bool
    succeeded: bool
    failure_reason: str | None
    # 费用（元），只用来显示；None 表示算不出来，不是 0（D37）。
    cost: float | None

    @property
    def budget_tokens(self) -> int:
        """计入预算的 token：输入加输出，缓存命中不重复计算。"""
        return self.prompt_tokens + self.completion_tokens


def month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """算出某个时刻所在的 UTC 自然月的起止。

    Args:
        moment: 带时区的时间。

    Returns:
        （本月第一刻, 下个月第一刻），前者包含、后者不包含，都是 UTC。

    Raises:
        ValueError: 时间不带时区。
    """
    utc_moment = ensure_aware(moment)
    start = datetime(utc_moment.year, utc_moment.month, 1, tzinfo=UTC)
    if utc_moment.month == _DECEMBER:
        end = datetime(utc_moment.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(utc_moment.year, utc_moment.month + 1, 1, tzinfo=UTC)
    return start, end


def _format_timestamp(moment: datetime) -> str:
    return ensure_aware(moment).isoformat(timespec=_TIMESTAMP_PRECISION)


class Ledger:
    """账本：只能追加和查询，不能修改、不能删除（触发器强制保证）。"""

    def __init__(self, db_path: Path, clock: Clock | None = None) -> None:
        """创建账本对象（还没有打开数据库）。

        Args:
            db_path: 数据库文件路径，通常是数据根目录里的 aureus.db。
            clock: 时钟对象，默认使用真实时钟。记账时间从它取。
        """
        self._db_path = db_path
        self._clock = clock or SystemClock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """打开数据库连接并初始化表结构。已经打开时什么也不做。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            self._init_schema(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        """建表、索引和触发器。CHECK 约束让不合理的数字在数据库层面就写不进去。"""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                organ_id TEXT NOT NULL,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL CHECK (prompt_tokens >= 0),
                completion_tokens INTEGER NOT NULL CHECK (completion_tokens >= 0),
                cached_tokens INTEGER NOT NULL CHECK (cached_tokens >= 0),
                estimated INTEGER NOT NULL CHECK (estimated IN (0, 1)),
                succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1)),
                failure_reason TEXT,
                cost REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_timestamp ON ledger(timestamp)")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS no_update_ledger
            BEFORE UPDATE ON ledger
            BEGIN
                SELECT RAISE(ABORT, 'Ledger records are immutable: UPDATE is not allowed');
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS no_delete_ledger
            BEFORE DELETE ON ledger
            BEGIN
                SELECT RAISE(ABORT, 'Ledger records are immutable: DELETE is not allowed');
            END
        """)
        # INSERT OR REPLACE 会先删掉冲突的行，而默认情况下这个删除不触发 DELETE 触发器，
        # 所以要在插入前拦住「主键已存在」（审查 S3）。新行不带主键时 NEW.id 是 NULL，不会误拦。
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS no_replace_ledger
            BEFORE INSERT ON ledger
            WHEN EXISTS (SELECT 1 FROM ledger WHERE id = NEW.id)
            BEGIN
                SELECT RAISE(ABORT, 'Ledger records are immutable: REPLACE is not allowed');
            END
        """)
        conn.commit()

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("账本还没有打开")
        return self._conn

    def record(self, organ_id: str, attempts: Sequence[CallAttempt]) -> None:
        """把一次器官调用里的所有模型请求记进账本。

        同一次调用的请求在一个事务里写入：要么全记上，要么全不记。

        Args:
            organ_id: 发起请求的器官。
            attempts: 这次调用里真正发出去的每一次模型请求。空列表什么也不写。

        Raises:
            RuntimeError: 账本没有打开。
            sqlite3.Error: 写入失败，这次的记录一条都没有写进去。
        """
        conn = self._require_open()
        if not attempts:
            return
        timestamp = _format_timestamp(self._clock.now())
        rows = [
            (
                timestamp,
                organ_id,
                attempt.model,
                attempt.provider,
                attempt.prompt_tokens,
                attempt.completion_tokens,
                attempt.cached_tokens,
                int(attempt.estimated),
                int(attempt.succeeded),
                attempt.failure_reason,
                attempt.cost,
            )
            for attempt in attempts
        ]
        # `with conn` 在成功时提交、出错时回滚。
        with conn:
            conn.executemany(
                """
                INSERT INTO ledger
                (timestamp, organ_id, model, provider, prompt_tokens, completion_tokens,
                 cached_tokens, estimated, succeeded, failure_reason, cost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def tokens_used_in_month(self, moment: datetime) -> int:
        """查某个 UTC 自然月已经用了多少预算 token（输入加输出）。

        Args:
            moment: 带时区的时间，用来确定是哪个月。

        Returns:
            这个月所有记录的输入 token 加输出 token 之和。

        Raises:
            RuntimeError: 账本没有打开。
            sqlite3.Error: 读取失败。
        """
        conn = self._require_open()
        start, end = month_bounds(moment)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS used
            FROM ledger WHERE timestamp >= ? AND timestamp < ?
            """,
            (_format_timestamp(start), _format_timestamp(end)),
        ).fetchone()
        return int(row["used"])

    def entries(self, since_id: int = 0, limit: int = 100) -> list[LedgerEntry]:
        """按 ID 升序读取记录。

        Args:
            since_id: 只返回 ID 比它大的记录。
            limit: 最多返回多少条。

        Raises:
            RuntimeError: 账本没有打开。
        """
        conn = self._require_open()
        rows = conn.execute(
            "SELECT * FROM ledger WHERE id > ? ORDER BY id ASC LIMIT ?", (since_id, limit)
        ).fetchall()
        return [
            LedgerEntry(
                id=row["id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                organ_id=row["organ_id"],
                model=row["model"],
                provider=row["provider"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                cached_tokens=row["cached_tokens"],
                estimated=bool(row["estimated"]),
                succeeded=bool(row["succeeded"]),
                failure_reason=row["failure_reason"],
                cost=row["cost"],
            )
            for row in rows
        ]
