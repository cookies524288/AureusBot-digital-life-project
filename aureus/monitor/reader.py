"""观察台的只读数据层（D64）：用 SQLite 只读连接读数据库，什么都不写。

这里不 import streamlit：读数据和画页面分开，测试不用开浏览器。
老数据库里还没有某张表（比如没有 body_samples）时当作没有数据，不报错。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureus.body import BodySample, sample_of
from aureus.common.clock import ensure_aware
from aureus.identity.history import IdentityLayer, IdentityVersion, VersionSource

# 和账本、样本表一致：带微秒的 UTC，字符串比较大小就等于比较时间。
_TIMESTAMP_PRECISION = "microseconds"
# 只读连接等锁的上限（秒）：心跳在写库时观察台最多等这么久，等不到就报错，不拖住页面。
_BUSY_TIMEOUT_SECONDS = 1.0


class MonitorError(Exception):
    """观察台读不了数据库（文件不存在等），消息是写给用户看的中文。"""


@dataclass(frozen=True)
class TimelineEntry:
    """时间线里的一条记录，观察台只显示这几项。"""

    id: int
    timestamp: datetime
    actor_type: str
    event_type: str
    content: str
    visibility: str


def _format(moment: datetime) -> str:
    return ensure_aware(moment).isoformat(timespec=_TIMESTAMP_PRECISION)


class MonitorReader:
    """只读地读数据库：身体样本、时间线、花费、人设历史。"""

    def __init__(self, db_path: Path) -> None:
        """创建读取器（还没有打开数据库）。"""
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """以只读方式打开数据库。已经打开时什么也不做。

        Raises:
            MonitorError: 数据库文件不存在，或者打不开。
        """
        if self._conn is not None:
            return
        if not self._db_path.is_file():
            raise MonitorError(f"找不到数据库 {self._db_path}，先用 aureus chat 运行一次。")
        uri = f"{self._db_path.as_uri()}?mode=ro"
        try:
            conn = sqlite3.connect(
                uri, uri=True, timeout=_BUSY_TIMEOUT_SECONDS, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            # 双保险：连接本身就是只读的，再让 SQLite 拒绝任何写入语句。
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error as error:
            raise MonitorError(f"打不开数据库 {self._db_path}：{error}") from None
        self._conn = conn

    def close(self) -> None:
        """关闭连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _rows(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        """执行只读查询；表还不存在时返回空列表。"""
        if self._conn is None:
            raise RuntimeError("观察台的读取器还没有打开")
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return []
            raise

    def latest_sample(self) -> BodySample | None:
        """最新的一行身体样本；还没有样本时是 None。"""
        rows = self._rows("SELECT * FROM body_samples ORDER BY id DESC LIMIT 1")
        return sample_of(rows[0]) if rows else None

    def samples_since(self, moment: datetime, limit: int = 2000) -> list[BodySample]:
        """从某个时刻起（含）的样本，从旧到新，最多 limit 行。"""
        rows = self._rows(
            "SELECT * FROM body_samples WHERE timestamp >= ? ORDER BY id ASC LIMIT ?",
            (_format(moment), limit),
        )
        return [sample_of(row) for row in rows]

    def recent_timeline(self, limit: int = 50) -> list[TimelineEntry]:
        """最新的几条时间线记录，从旧到新。观察台只绑本机，所以不按可见范围过滤。"""
        rows = self._rows(
            "SELECT id, timestamp, actor_type, event_type, content, visibility "
            "FROM timeline ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            TimelineEntry(
                id=int(row["id"]),
                timestamp=datetime.fromisoformat(row["timestamp"]),
                actor_type=str(row["actor_type"]),
                event_type=str(row["event_type"]),
                content=str(row["content"]),
                visibility=str(row["visibility"]),
            )
            for row in reversed(rows)
        ]

    def tokens_used(self, start: datetime, end: datetime) -> int:
        """起止之间（含起不含止）账本里的预算 token 之和（输入加输出）。

        一天、一个月的边界由调用方按预算日历算好传进来，这里不猜时区。
        """
        rows = self._rows(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS used "
            "FROM ledger WHERE timestamp >= ? AND timestamp < ?",
            (_format(start), _format(end)),
        )
        return int(rows[0]["used"]) if rows else 0

    def identity_versions(self, limit: int = 30) -> list[IdentityVersion]:
        """人设历史里最新的几个版本，从旧到新，所有层混在一起。"""
        rows = self._rows(
            "SELECT id, layer, content, source, created_at FROM identity_versions "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            IdentityVersion(
                id=int(row["id"]),
                layer=IdentityLayer(row["layer"]),
                content=str(row["content"]),
                source=VersionSource(row["source"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in reversed(rows)
        ]
