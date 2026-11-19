"""反馈存储的测试（路线图 2.1，D67）：记预期、补结果、不许改、算自信。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import FeedbackConfig, ManualClock
from aureus.feedback import (
    PRIOR_CONFIDENCE,
    FeedbackError,
    FeedbackStore,
    Outcome,
    check_category,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
CATEGORY = "sim_task:easy"


def make_store(
    tmp_path: Path, *, window_size: int = 50, min_samples: int = 10
) -> tuple[FeedbackStore, ManualClock]:
    clock = ManualClock(NOW)
    config = FeedbackConfig(window_size=window_size, min_samples=min_samples)
    store = FeedbackStore(tmp_path / "aureus.db", config, clock)
    store.open()
    return store, clock


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FeedbackStore]:
    opened, _ = make_store(tmp_path)
    yield opened
    opened.close()


def add_results(store: FeedbackStore, outcomes: list[Outcome], category: str = CATEGORY) -> None:
    for outcome in outcomes:
        record_id = store.record_expectation(category, 0.5)
        if outcome is not Outcome.UNKNOWN:
            store.record_outcome(record_id, outcome)


# ---------- 类别 ----------


def test_category_must_be_known() -> None:
    assert check_category("sim_task:easy") == "sim_task:easy"
    for bad in ("", "sim_task:", "sim_task:   ", "reply_accepted", "任意起的名字"):
        with pytest.raises(FeedbackError):
            check_category(bad)


def test_store_rejects_unknown_category(store: FeedbackStore) -> None:
    with pytest.raises(FeedbackError):
        store.record_expectation("乱起的名字", 0.5)
    with pytest.raises(FeedbackError):
        store.confidence("乱起的名字")


# ---------- 记预期和补结果 ----------


def test_expectation_is_stored_with_unknown_outcome(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    record_id = store.record_expectation(CATEGORY, 0.7, source_id=42)
    record = store.get(record_id)
    assert record is not None
    assert (record.category, record.expected, record.outcome) == (CATEGORY, 0.7, Outcome.UNKNOWN)
    assert (record.expected_at, record.outcome_at, record.source_id) == (NOW, None, 42)
    assert record.impulsive is False
    clock.advance(timedelta(minutes=3))
    store.record_outcome(record_id, Outcome.SUCCESS)
    done = store.get(record_id)
    assert done is not None
    assert done.outcome is Outcome.SUCCESS
    assert done.outcome_at == NOW + timedelta(minutes=3)
    assert done.expected_at == NOW  # 预期时间不随补结果改变
    store.close()


def test_missing_expectation_is_recorded_as_impulsive(store: FeedbackStore) -> None:
    record = store.get(store.record_expectation(CATEGORY, None))
    assert record is not None
    assert record.expected is None
    assert record.impulsive is True


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan")])
def test_expectation_must_be_between_zero_and_one(store: FeedbackStore, bad: float) -> None:
    with pytest.raises(FeedbackError):
        store.record_expectation(CATEGORY, bad)


def test_outcome_can_only_be_set_once(store: FeedbackStore) -> None:
    record_id = store.record_expectation(CATEGORY, 0.5)
    store.record_outcome(record_id, Outcome.FAILURE)
    with pytest.raises(FeedbackError):
        store.record_outcome(record_id, Outcome.SUCCESS)
    record = store.get(record_id)
    assert record is not None
    assert record.outcome is Outcome.FAILURE


def test_unknown_outcome_is_not_written(store: FeedbackStore) -> None:
    record_id = store.record_expectation(CATEGORY, 0.5)
    with pytest.raises(FeedbackError):
        store.record_outcome(record_id, Outcome.UNKNOWN)


def test_outcome_of_missing_record_is_an_error(store: FeedbackStore) -> None:
    with pytest.raises(FeedbackError):
        store.record_outcome(999, Outcome.SUCCESS)


def test_use_before_open_is_an_error(tmp_path: Path) -> None:
    unopened = FeedbackStore(tmp_path / "aureus.db")
    with pytest.raises(RuntimeError):
        unopened.record_expectation(CATEGORY, 0.5)


# ---------- 数据库层面不许改 ----------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE feedback_records SET expected = 0.99",
        "UPDATE feedback_records SET category = 'sim_task:other'",
        "UPDATE feedback_records SET expected_at = '2000-01-01T00:00:00.000000+00:00'",
        "UPDATE feedback_records SET source_id = 7",
        "UPDATE feedback_records SET id = 99",
        "UPDATE feedback_records SET outcome = 'success'",  # 已经有结果，不许再改
        "UPDATE feedback_records SET outcome = 'unknown'",  # 不许把结果擦掉
        "DELETE FROM feedback_records",
        "INSERT OR REPLACE INTO feedback_records (id, category, outcome, expected_at)"
        " VALUES (1, 'sim_task:easy', 'success', '2026-11-19T12:00:00.000000+00:00')",
    ],
)
def test_database_triggers_block_rewriting(tmp_path: Path, statement: str) -> None:
    store, _ = make_store(tmp_path)
    record_id = store.record_expectation(CATEGORY, 0.5)
    store.record_outcome(record_id, Outcome.FAILURE)
    store.close()
    conn = sqlite3.connect(tmp_path / "aureus.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement)
    conn.close()


def test_database_blocks_changing_expectation_while_outcome_is_unknown(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.record_expectation(CATEGORY, 0.5)
    store.close()
    conn = sqlite3.connect(tmp_path / "aureus.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE feedback_records SET expected = 0.9")
    conn.close()


def test_reopening_keeps_records(tmp_path: Path) -> None:
    first, _ = make_store(tmp_path)
    record_id = first.record_expectation(CATEGORY, 0.4)
    first.close()
    second, _ = make_store(tmp_path)
    record = second.get(record_id)
    assert record is not None
    assert record.expected == 0.4
    second.close()


# ---------- 自信 ----------


def test_confidence_without_history_is_no_data(store: FeedbackStore) -> None:
    confidence = store.confidence(CATEGORY)
    assert confidence.value is None
    assert confidence.samples == 0
    assert "没有数据" in confidence.describe()


def test_unknown_outcomes_are_not_counted(store: FeedbackStore) -> None:
    add_results(store, [Outcome.UNKNOWN] * 5)
    assert store.confidence(CATEGORY).value is None


def test_few_samples_are_pulled_toward_the_prior(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, min_samples=10)
    add_results(store, [Outcome.SUCCESS] * 2)  # 2 次成功，补 8 次先验 0.5
    confidence = store.confidence(CATEGORY)
    assert confidence.insufficient is True
    assert confidence.value == pytest.approx((2 + PRIOR_CONFIDENCE * 8) / 10)
    assert confidence.samples == 2
    assert "样本不足" in confidence.describe()
    store.close()


def test_enough_samples_use_the_raw_success_rate(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, min_samples=10)
    add_results(store, [Outcome.SUCCESS] * 3 + [Outcome.FAILURE] * 7)
    confidence = store.confidence(CATEGORY)
    assert confidence.insufficient is False
    assert confidence.value == pytest.approx(0.3)
    assert "样本不足" not in confidence.describe()
    store.close()


def test_only_the_latest_window_counts(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, window_size=10, min_samples=10)
    add_results(store, [Outcome.FAILURE] * 30 + [Outcome.SUCCESS] * 10)
    confidence = store.confidence(CATEGORY)
    assert confidence.samples == 10
    assert confidence.value == pytest.approx(1.0)  # 连续失败后又连续成功，窗口里只剩成功
    store.close()


def test_categories_are_counted_separately(store: FeedbackStore) -> None:
    add_results(store, [Outcome.SUCCESS] * 10, "sim_task:easy")
    add_results(store, [Outcome.FAILURE] * 10, "sim_task:hard")
    assert store.confidence("sim_task:easy").value == pytest.approx(1.0)
    assert store.confidence("sim_task:hard").value == pytest.approx(0.0)
    assert store.confidence("sim_task:other").value is None


def test_impulsive_records_still_count_their_outcome(store: FeedbackStore) -> None:
    # 冲动（没写预期）不影响自信统计：自信只看真实结果，和当初写没写预期无关。
    for outcome in (Outcome.SUCCESS, Outcome.FAILURE):
        record_id = store.record_expectation(CATEGORY, None)
        store.record_outcome(record_id, outcome)
    assert store.confidence(CATEGORY).samples == 2
