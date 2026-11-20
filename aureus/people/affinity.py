"""人际 people：好感（路线图 2.4，D66 第 3 条、D71）。

好感满分 100，越高越难涨、越低越难跌：涨幅 = 基础变化量 × (1 − 好感 ÷ 100)，
跌幅 = 基础变化量 × (好感 ÷ 100)，所以永远到不了 0 和 100。
每次变化追加一行到表 affinity_changes（只属于这个子系统，只追加，触发器禁止改和删），
当前好感取这个人最新一行的「变化后的值」，没有就是配置里的初值。
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from aureus.common import BudgetCalendar, Clock, PeopleConfig
from aureus.common.sqlite import open_connection

_TIMESTAMP_PRECISION = "microseconds"
FULL_AFFINITY = 100.0
# 变化小于这个量就不记：好感接近两端时打折后几乎为零，记下来只是噪音。
_NEGLIGIBLE_CHANGE = 1e-9


class AffinityReason(StrEnum):
    """好感为什么变。"""

    PRAISE = "praise"  # 夸它
    OFFENSE = "offense"  # 冒犯它
    REJECTION = "rejection"  # 拒绝或冷落它
    INTERACTION = "interaction"  # 当天有过对话（互动量）


@dataclass(frozen=True)
class AffinityEvent:
    """判断器官报出的一件冲着它来的事，已经由心跳换成人际认得的说法。

    强度和把握都在 0 到 1 之间（判断器官的解析已经校验过）。
    """

    reason: AffinityReason
    intensity: float
    confidence: float


@dataclass(frozen=True)
class AffinityChange:
    """好感的一次变化，对应 affinity_changes 表的一行。"""

    person_id: int
    timestamp: datetime
    delta: float  # 正数是涨，负数是跌
    value: float  # 变化后的好感
    reason: AffinityReason
    source_record_id: int | None  # 来源时间线记录（判断记录）的序号


def rise(value: float, amount: float) -> float:
    """在好感 value 上涨 amount（基础量）实际涨多少：越高越难涨。"""
    return amount * (1 - value / FULL_AFFINITY)


def fall(value: float, amount: float) -> float:
    """在好感 value 上跌 amount（基础量）实际跌多少（正数）：越低越难跌。"""
    return amount * (value / FULL_AFFINITY)


class AffinityBook:
    """好感账：查当前好感，按判断结果记变化。

    时间都从时钟取；「一天」按 D55 的边界算，和每日额度一致。
    """

    def __init__(
        self,
        db_path: Path,
        clock: Clock,
        config: PeopleConfig,
        calendar: BudgetCalendar,
        *,
        min_confidence: float,
    ) -> None:
        """创建好感账。

        Args:
            db_path: aureus.db 的路径；人物档案要先建过表（这里按 people 表认用户）。
            clock: 时钟。
            config: 人际配置（初值、各项基础量、每日上限）。
            calendar: 一天从几点开始（D55）。
            min_confidence: 把握低于它的判断不算，和激素用同一个线（D71 第 2 条）。
        """
        self._db_path = db_path
        self._clock = clock
        self._config = config
        self._calendar = calendar
        self._min_confidence = min_confidence
        self._conn: sqlite3.Connection | None = None

    @property
    def config(self) -> PeopleConfig:
        """人际配置。"""
        return self._config

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS affinity_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                person_id INTEGER NOT NULL,
                delta REAL NOT NULL,
                value REAL NOT NULL CHECK (value > 0 AND value < 100),
                reason TEXT NOT NULL
                    CHECK (reason IN ('praise', 'offense', 'rejection', 'interaction')),
                source_record_id INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS affinity_by_person "
            "ON affinity_changes(person_id, timestamp)"
        )
        # 只追加：每次变化都能回头查原因，事后改不了（D71 第 4 条）。
        for action in ("UPDATE", "DELETE"):
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS affinity_changes_no_{action.lower()}
                BEFORE {action} ON affinity_changes
                BEGIN SELECT RAISE(ABORT, '好感变化只能追加，不能修改或删除'); END
                """
            )
        conn.commit()

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("好感账还没有打开")
        return self._conn

    def current(self, person_id: int) -> float:
        """这个人此刻的好感；还没有变化时是初值（用户和其他人各一个）。"""
        conn = self._require_open()
        return self._current(conn, person_id)

    def _current(self, conn: sqlite3.Connection, person_id: int) -> float:
        row = conn.execute(
            "SELECT value FROM affinity_changes WHERE person_id = ? ORDER BY id DESC LIMIT 1",
            (person_id,),
        ).fetchone()
        if row is not None:
            return float(row["value"])
        user = conn.execute(
            "SELECT 1 FROM people WHERE id = ? AND kind = 'user'", (person_id,)
        ).fetchone()
        if user is not None:
            return self._config.user_initial_affinity
        return self._config.stranger_affinity

    def history(self, person_id: int) -> list[AffinityChange]:
        """这个人的全部好感变化，按先后。"""
        conn = self._require_open()
        rows = conn.execute(
            "SELECT person_id, timestamp, delta, value, reason, source_record_id "
            "FROM affinity_changes WHERE person_id = ? ORDER BY id",
            (person_id,),
        ).fetchall()
        return [_change_from_row(row) for row in rows]

    def record_judgment(
        self, person_id: int, events: Sequence[AffinityEvent], source_record_id: int | None
    ) -> list[AffinityChange]:
        """按一次判断过的对话记好感变化（D71 第 1、2 条），返回实际记下的变化。

        当天第一次判断过的对话先按互动量涨一小份，再逐件算判断器官报的事。
        把握不够的事不算；涨、跌各自受当天上限约束，到了上限就截断或不记。
        一次判断的变化在同一个事务里写入，要么全写上，要么一条都不留。

        Args:
            person_id: 发这条消息的人。
            events: 判断器官报出的冲着它来的事；可以为空（只算互动量）。
            source_record_id: 判断记录在时间线里的序号。
        """
        conn = self._require_open()
        now = self._clock.now().astimezone(UTC)
        start, end = self._calendar.day_bounds(now)
        changes: list[AffinityChange] = []
        with conn:
            value = self._current(conn, person_id)
            rose, fell, interacted = self._today(conn, person_id, start, end)
            planned: list[tuple[AffinityReason, float]] = []
            if not interacted:
                planned.append((AffinityReason.INTERACTION, self._config.interaction_amount))
            for event in events:
                if event.confidence < self._min_confidence:
                    continue
                amount = self._amount(event) * event.intensity * event.confidence
                planned.append((event.reason, amount))
            for reason, amount in planned:
                # 到了当天上限就截断；截断到零的不记。
                if _is_fall(reason):
                    size = max(min(fall(value, amount), self._config.daily_fall_cap - fell), 0.0)
                    fell += size
                    delta = -size
                else:
                    size = max(min(rise(value, amount), self._config.daily_rise_cap - rose), 0.0)
                    rose += size
                    delta = size
                if size < _NEGLIGIBLE_CHANGE:
                    continue
                value = _strictly_inside(value + delta)
                change = AffinityChange(person_id, now, delta, value, reason, source_record_id)
                self._insert(conn, change)
                changes.append(change)
        return changes

    def _amount(self, event: AffinityEvent) -> float:
        config = self._config
        return {
            AffinityReason.PRAISE: config.praise_amount,
            AffinityReason.OFFENSE: config.offense_amount,
            AffinityReason.REJECTION: config.rejection_amount,
            AffinityReason.INTERACTION: config.interaction_amount,
        }[event.reason]

    @staticmethod
    def _today(
        conn: sqlite3.Connection, person_id: int, start: datetime, end: datetime
    ) -> tuple[float, float, bool]:
        """当天已经涨了多少、跌了多少（正数）、记过互动量没有。"""
        rows = conn.execute(
            "SELECT delta, reason FROM affinity_changes "
            "WHERE person_id = ? AND timestamp >= ? AND timestamp < ?",
            (person_id, _stamp(start), _stamp(end)),
        ).fetchall()
        rose = sum(float(row["delta"]) for row in rows if row["delta"] > 0)
        fell = -sum(float(row["delta"]) for row in rows if row["delta"] < 0)
        interacted = any(row["reason"] == AffinityReason.INTERACTION.value for row in rows)
        return rose, fell, interacted

    @staticmethod
    def _insert(conn: sqlite3.Connection, change: AffinityChange) -> None:
        conn.execute(
            "INSERT INTO affinity_changes "
            "(timestamp, person_id, delta, value, reason, source_record_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _stamp(change.timestamp),
                change.person_id,
                change.delta,
                change.value,
                change.reason.value,
                change.source_record_id,
            ),
        )


def describe_affinity(value: float, *, is_user: bool) -> str:
    """放进系统上下文的一行事实（D71 第 5 条）。"""
    who = "用户" if is_user else "这个人"
    return (
        f"【好感】\n你对{who}的好感：{value:.1f} / 100（越高越难涨、越低越难跌，来自真实的互动）。"
    )


def _is_fall(reason: AffinityReason) -> bool:
    return reason in (AffinityReason.OFFENSE, AffinityReason.REJECTION)


def _strictly_inside(value: float) -> float:
    """浮点舍入兜底：公式上到不了两端，这里保证存进去的值一定在 (0, 100) 里。"""
    return min(max(value, _NEGLIGIBLE_CHANGE), FULL_AFFINITY - _NEGLIGIBLE_CHANGE)


def _stamp(moment: datetime) -> str:
    # 统一换成 UTC 再存，按字符串比较先后才可靠。
    return moment.astimezone(UTC).isoformat(timespec=_TIMESTAMP_PRECISION)


def _change_from_row(row: sqlite3.Row) -> AffinityChange:
    source = row["source_record_id"]
    return AffinityChange(
        person_id=int(row["person_id"]),
        timestamp=datetime.fromisoformat(row["timestamp"]),
        delta=float(row["delta"]),
        value=float(row["value"]),
        reason=AffinityReason(row["reason"]),
        source_record_id=None if source is None else int(source),
    )
