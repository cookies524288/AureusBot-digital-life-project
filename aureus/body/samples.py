"""身体信号样本：心跳定时记一行读数，观察台画曲线靠它（D64）。

表 `body_samples` 只属于身体子系统，放在 aureus.db 里。和账本、时间线不同，它不是只追加的：
样本只是读数、不是经历，超过保留期限的行要清掉（D64 第 2 条，14 号文档），这是有意的例外。
关掉的信号存 NULL 而不是 0，曲线上显示为断开，不装成「正常」。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from aureus.body.body import BodyReading
from aureus.common import Clock, SystemClock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import open_connection

# 时间固定写成带微秒的 UTC，格式等宽，SQLite 里按字符串比较大小就等于按时间比较（同账本）。
_TIMESTAMP_PRECISION = "microseconds"


@dataclass(frozen=True)
class BodySample:
    """某一刻的身体读数。None 表示这个信号当时关着或不限，不是 0。"""

    timestamp: datetime
    # 精力档位的中文名（含「不限」）；比例等数字在不限时是 None。
    energy_tier: str
    energy_fraction: float | None
    used_today: int | None
    daily_quota: int | None
    fatigue_level: float | None
    stress_level: float | None
    night: bool | None
    # 注意力聚焦在哪（认知状态的 attention_focus）；空串表示没有。
    focus: str
    # 收件箱里排队的条数。只存条数，不存发件人和内容（里面有私聊，D64 第 3 条）。
    inbox_count: int


def sample_from_reading(
    moment: datetime, reading: BodyReading, *, focus: str, inbox_count: int
) -> BodySample:
    """把一份身体读数换成样本；关掉的信号存 None。"""
    energy, fatigue = reading.energy, reading.fatigue
    stress, sleep = reading.stress, reading.sleep
    return BodySample(
        timestamp=moment,
        energy_tier=energy.tier.value,
        energy_fraction=energy.fraction,
        used_today=energy.used_today,
        daily_quota=energy.daily_quota,
        fatigue_level=fatigue.level if fatigue.enabled else None,
        stress_level=stress.level if stress.enabled else None,
        night=sleep.night if sleep.enabled else None,
        focus=focus,
        inbox_count=max(inbox_count, 0),
    )


def _format_timestamp(moment: datetime) -> str:
    return ensure_aware(moment).isoformat(timespec=_TIMESTAMP_PRECISION)


class BodySampleStore:
    """样本存储：追加、清理过期、读最新和最近一段。"""

    def __init__(self, db_path: Path, clock: Clock | None = None) -> None:
        """创建存储对象（还没有打开数据库）。清理过期用的「现在」从时钟取。"""
        self._db_path = db_path
        self._clock = clock or SystemClock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """打开数据库连接并建表。已经打开时什么也不做。"""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS body_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                energy_tier TEXT NOT NULL,
                energy_fraction REAL CHECK (energy_fraction BETWEEN 0 AND 1),
                used_today INTEGER CHECK (used_today >= 0),
                daily_quota INTEGER CHECK (daily_quota >= 0),
                fatigue_level REAL CHECK (fatigue_level BETWEEN 0 AND 1),
                stress_level REAL CHECK (stress_level BETWEEN 0 AND 1),
                backlog INTEGER CHECK (backlog >= 0),
                night INTEGER CHECK (night IN (0, 1)),
                focus TEXT NOT NULL,
                inbox_count INTEGER NOT NULL CHECK (inbox_count >= 0)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_body_samples_timestamp ON body_samples(timestamp)"
        )
        conn.commit()

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("样本存储还没有打开")
        return self._conn

    def append(self, sample: BodySample) -> None:
        """追加一行样本。"""
        conn = self._require_open()
        night = None if sample.night is None else int(sample.night)
        conn.execute(
            "INSERT INTO body_samples (timestamp, energy_tier, energy_fraction, used_today,"
            " daily_quota, fatigue_level, stress_level, night, focus, inbox_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _format_timestamp(sample.timestamp),
                sample.energy_tier,
                sample.energy_fraction,
                sample.used_today,
                sample.daily_quota,
                sample.fatigue_level,
                sample.stress_level,
                night,
                sample.focus,
                sample.inbox_count,
            ),
        )
        conn.commit()

    def prune(self, retention_days: int) -> int:
        """删掉早于保留期限的样本，返回删了多少行。"""
        conn = self._require_open()
        cutoff = self._clock.now() - timedelta(days=retention_days)
        cursor = conn.execute(
            "DELETE FROM body_samples WHERE timestamp < ?", (_format_timestamp(cutoff),)
        )
        conn.commit()
        return cursor.rowcount

    def latest(self) -> BodySample | None:
        """最新的一行；还没有样本时返回 None。"""
        row = (
            self._require_open()
            .execute("SELECT * FROM body_samples ORDER BY id DESC LIMIT 1")
            .fetchone()
        )
        return None if row is None else sample_of(row)

    def since(self, moment: datetime, limit: int = 1000) -> list[BodySample]:
        """从某个时刻起（含）的样本，从旧到新，最多 limit 行。"""
        rows = (
            self._require_open()
            .execute(
                "SELECT * FROM body_samples WHERE timestamp >= ? ORDER BY id ASC LIMIT ?",
                (_format_timestamp(moment), limit),
            )
            .fetchall()
        )
        return [sample_of(row) for row in rows]


def sample_of(row: sqlite3.Row) -> BodySample:
    """把数据库的一行换成样本（观察台的只读连接也用它）。"""
    night = row["night"]
    return BodySample(
        timestamp=datetime.fromisoformat(row["timestamp"]),
        energy_tier=row["energy_tier"],
        energy_fraction=row["energy_fraction"],
        used_today=row["used_today"],
        daily_quota=row["daily_quota"],
        fatigue_level=row["fatigue_level"],
        stress_level=row["stress_level"],
        night=None if night is None else bool(night),
        focus=row["focus"],
        inbox_count=row["inbox_count"],
    )
