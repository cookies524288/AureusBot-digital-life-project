"""反馈：做事前记预期，做完记结果，用真实成功率算「自信」（D67，03 号文档）。

表 `feedback_records` 只属于反馈子系统，放在 aureus.db 里。它是统计依据、不是经历，
所以结果允许从「未知」补写一次；但预期写入后不许改，数据库触发器强制保证，
防止事后改预期来美化成绩。不许删除。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from aureus.common import Clock, FeedbackConfig, SystemClock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import open_connection

# 时间固定写成带微秒的 UTC，格式等宽，SQLite 里按字符串比较大小就等于按时间比较（同账本）。
_TIMESTAMP_PRECISION = "microseconds"
# 没有任何历史、或历史不够时，自信向这个先验靠拢：不知道就是五五开，不装作有把握。
PRIOR_CONFIDENCE = 0.5
# 模拟任务类别的前缀（2.1 只有这一种类别，D67 第 2 条）。
SIM_TASK_PREFIX = "sim_task:"
# 承诺有没有兑现（D74 第 6 条）：答应的事做完算成功，放弃或过期算失败。
COMMITMENT_KEPT = "commitment_kept"
# 代码认识的固定类别（D67 第 2 条）；模拟任务另按前缀认。
_FIXED_CATEGORIES = frozenset({COMMITMENT_KEPT})


class Outcome(StrEnum):
    """一次行动的结果。UNKNOWN 是还没有结果，不当成功也不当失败。"""

    UNKNOWN = "unknown"
    SUCCESS = "success"
    FAILURE = "failure"


class FeedbackError(Exception):
    """反馈记录违反规则（类别不合法、预期越界、重复补写结果等）。"""


@dataclass(frozen=True)
class FeedbackRecord:
    """反馈表里的一行。"""

    id: int
    category: str
    # 写下的预期把握 [0, 1]；冲动（没写预期就行动）时是 None。
    expected: float | None
    outcome: Outcome
    expected_at: datetime
    outcome_at: datetime | None
    # 来源时间线记录的序号；没有对应时间线记录（比如模拟任务）时是 None。
    source_id: int | None

    @property
    def impulsive(self) -> bool:
        """没写预期就行动。"""
        return self.expected is None


@dataclass(frozen=True)
class Confidence:
    """某一类事情的自信。"""

    category: str
    # 自信 [0, 1]；还没有任何有结果的历史时是 None，显示「没有数据」。
    value: float | None
    # 算进去的有结果的次数。
    samples: int
    # 样本不足 min_samples：value 已经向先验靠拢，不能当成可靠的数字。
    insufficient: bool

    def describe(self) -> str:
        """给人看的一句话。没有数据就如实说没有数据。"""
        if self.value is None:
            return f"{self.category}：没有数据"
        note = "（样本不足）" if self.insufficient else ""
        return f"{self.category}：{self.value:.0%}，根据最近 {self.samples} 次{note}"


def check_category(category: str) -> str:
    """类别必须是代码认识的：模拟任务 sim_task:<名字>，和固定类别（D67 第 2 条、D74）。

    Raises:
        FeedbackError: 类别不认识，或模拟任务没有名字。
    """
    if category in _FIXED_CATEGORIES:
        return category
    if category.startswith(SIM_TASK_PREFIX) and category[len(SIM_TASK_PREFIX) :].strip():
        return category
    raise FeedbackError(f"不认识的反馈类别：{category!r}（类别由代码固定，器官不能自己起名）")


def _format_timestamp(moment: datetime) -> str:
    return ensure_aware(moment).isoformat(timespec=_TIMESTAMP_PRECISION)


class FeedbackStore:
    """反馈存储：记预期、补结果、算自信。"""

    def __init__(
        self, db_path: Path, config: FeedbackConfig | None = None, clock: Clock | None = None
    ) -> None:
        """创建存储对象（还没有打开数据库）。

        Args:
            db_path: 数据库文件路径，通常是数据根目录里的 aureus.db。
            config: 窗口大小和样本下限；不给就用默认值。
            clock: 时钟对象，默认真实时钟。预期和结果的时间从它取。
        """
        self._db_path = db_path
        self._config = config or FeedbackConfig()
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

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("反馈存储还没有打开")
        return self._conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        outcomes = ", ".join(f"'{outcome.value}'" for outcome in Outcome)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS feedback_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                expected REAL CHECK (expected BETWEEN 0 AND 1),
                outcome TEXT NOT NULL CHECK (outcome IN ({outcomes})),
                expected_at TEXT NOT NULL,
                outcome_at TEXT,
                source_id INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_category ON feedback_records(category, id)"
        )
        # 预期、类别、时间、来源写入后一个字都不许改；结果只能从 unknown 变成
        # success 或 failure 一次，已经有结果的不许再改，也不许改回 unknown。
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS no_rewrite_feedback_records
            BEFORE UPDATE ON feedback_records
            WHEN OLD.category IS NOT NEW.category
              OR OLD.expected IS NOT NEW.expected
              OR OLD.expected_at IS NOT NEW.expected_at
              OR OLD.source_id IS NOT NEW.source_id
              OR OLD.id IS NOT NEW.id
              OR OLD.outcome != 'unknown'
              OR NEW.outcome = 'unknown'
            BEGIN
                SELECT RAISE(ABORT, 'Feedback expectations are immutable; outcome can be set once');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS no_delete_feedback_records
            BEFORE DELETE ON feedback_records
            BEGIN
                SELECT RAISE(ABORT, 'Feedback records are immutable: DELETE is not allowed');
            END
            """
        )
        # INSERT OR REPLACE 会先删掉冲突的行，默认不触发 DELETE 触发器，单独拦（同账本，审查 S3）。
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS no_replace_feedback_records
            BEFORE INSERT ON feedback_records
            WHEN EXISTS (SELECT 1 FROM feedback_records WHERE id = NEW.id)
            BEGIN
                SELECT RAISE(ABORT, 'Feedback records are immutable: REPLACE is not allowed');
            END
            """
        )
        conn.commit()

    def record_expectation(
        self, category: str, expected: float | None, source_id: int | None = None
    ) -> int:
        """行动前记下预期，返回这条记录的编号。

        Args:
            category: 类别，必须是代码认识的。
            expected: 预期把握 [0, 1]；None 表示没写预期就行动，记为冲动。
            source_id: 来源时间线记录的序号，没有就不填。

        Raises:
            FeedbackError: 类别不认识，或预期不在 [0, 1] 之间。
        """
        check_category(category)
        if expected is not None and not 0.0 <= expected <= 1.0:
            raise FeedbackError(f"预期把握要在 0 到 1 之间，收到 {expected}")
        conn = self._require_open()
        cursor = conn.execute(
            "INSERT INTO feedback_records (category, expected, outcome, expected_at, source_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                category,
                expected,
                Outcome.UNKNOWN.value,
                _format_timestamp(self._clock.now()),
                source_id,
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def record_outcome(self, record_id: int, outcome: Outcome) -> None:
        """补写结果，只能写一次。结果不明时不要调用：它本来就是「未知」，不臆测成功。

        Raises:
            FeedbackError: 结果不是成功或失败，记录不存在，或已经有结果了。
        """
        if outcome is Outcome.UNKNOWN:
            raise FeedbackError("结果不明就不要补写：它本来就是未知，不臆测成功")
        conn = self._require_open()
        try:
            cursor = conn.execute(
                "UPDATE feedback_records SET outcome = ?, outcome_at = ? WHERE id = ?",
                (outcome.value, _format_timestamp(self._clock.now()), record_id),
            )
        except sqlite3.IntegrityError:
            raise FeedbackError(f"第 {record_id} 条反馈已经有结果了，结果只能补写一次") from None
        if cursor.rowcount == 0:
            raise FeedbackError(f"没有第 {record_id} 条反馈记录")
        conn.commit()

    def get(self, record_id: int) -> FeedbackRecord | None:
        """按编号取一行；没有就是 None。"""
        row = (
            self._require_open()
            .execute("SELECT * FROM feedback_records WHERE id = ?", (record_id,))
            .fetchone()
        )
        return None if row is None else _record_of(row)

    def outcomes_since(self, moment: datetime) -> list[FeedbackRecord]:
        """从某个时刻起（含）补写了结果的记录，按结果时间先后；结果未知的不在里面。

        给心情用（D73 第 2 条）：成功和失败按补写结果的时刻算。
        """
        rows = (
            self._require_open()
            .execute(
                "SELECT * FROM feedback_records WHERE outcome != ? AND outcome_at >= ?"
                " ORDER BY outcome_at, id",
                (Outcome.UNKNOWN.value, _format_timestamp(moment)),
            )
            .fetchall()
        )
        return [_record_of(row) for row in rows]

    def confidence(self, category: str) -> Confidence:
        """某一类事情的自信：最近 window_size 次有结果的成功率。

        结果未知的不计入分母。有结果的次数少于 min_samples 时，与先验按样本数加权混合
        （n 次真实结果加 min_samples - n 次先验），并标「样本不足」；一次都没有就是 None。

        Raises:
            FeedbackError: 类别不认识。
        """
        check_category(category)
        rows = (
            self._require_open()
            .execute(
                "SELECT outcome FROM feedback_records WHERE category = ? AND outcome != ?"
                " ORDER BY id DESC LIMIT ?",
                (category, Outcome.UNKNOWN.value, self._config.window_size),
            )
            .fetchall()
        )
        samples = len(rows)
        if samples == 0:
            return Confidence(category, None, 0, insufficient=True)
        successes = sum(1 for row in rows if row["outcome"] == Outcome.SUCCESS.value)
        missing = max(self._config.min_samples - samples, 0)
        value = (successes + PRIOR_CONFIDENCE * missing) / (samples + missing)
        return Confidence(category, value, samples, insufficient=missing > 0)


def _record_of(row: sqlite3.Row) -> FeedbackRecord:
    outcome_at = row["outcome_at"]
    return FeedbackRecord(
        id=int(row["id"]),
        category=str(row["category"]),
        expected=row["expected"],
        outcome=Outcome(row["outcome"]),
        expected_at=datetime.fromisoformat(row["expected_at"]),
        outcome_at=None if outcome_at is None else datetime.fromisoformat(outcome_at),
        source_id=row["source_id"],
    )
