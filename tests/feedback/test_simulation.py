"""模拟任务和校准误差的测试（路线图 2.1 步骤 5，D67 第 4 条验收）。

验收：成功率事先已知的模拟任务上，它说的把握逐渐接近真实成功率，ECE 随次数下降。
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import FeedbackConfig, ManualClock
from aureus.feedback import (
    PRIOR_CONFIDENCE,
    FeedbackStore,
    SimulatedAttempt,
    SimulatedTask,
    brier_score,
    calibration_bins,
    expected_calibration_error,
    run_simulation,
)

START = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)

TASKS = [SimulatedTask("hard", 0.3), SimulatedTask("mid", 0.7), SimulatedTask("easy", 0.9)]

# 跑几百次：每类 300 次，远多于窗口 50 次。
ROUNDS = 300
EARLY_ROUNDS = 10
LATE_ROUNDS = 100
# 多个种子上都要成立，不靠碰巧的一个种子。20 个种子的实测：后期 ECE 最大 0.093，
# 单类自信与真实成功率的差距最大 0.12；这里留出余量。
MAX_LATE_ECE = 0.12
MAX_CONFIDENCE_GAP = 0.2


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FeedbackStore]:
    opened = FeedbackStore(tmp_path / "aureus.db", FeedbackConfig(), ManualClock(START))
    opened.open()
    yield opened
    opened.close()


@pytest.mark.parametrize("seed", [1, 20261119])
def test_calibration_error_shrinks_as_history_grows(store: FeedbackStore, seed: int) -> None:
    attempts = run_simulation(store, TASKS, ROUNDS, seed)
    count = len(TASKS)

    early = expected_calibration_error(attempts[: EARLY_ROUNDS * count])
    late = expected_calibration_error(attempts[-LATE_ROUNDS * count :])

    assert late < early
    assert late < MAX_LATE_ECE


@pytest.mark.parametrize("seed", [1, 20261119])
def test_confidence_converges_to_the_true_success_rate(store: FeedbackStore, seed: int) -> None:
    run_simulation(store, TASKS, ROUNDS, seed)

    for task in TASKS:
        confidence = store.confidence(task.category)
        assert confidence.value is not None
        assert not confidence.insufficient
        assert abs(confidence.value - task.success_rate) < MAX_CONFIDENCE_GAP


def test_every_attempt_is_recorded_with_its_outcome(store: FeedbackStore) -> None:
    attempts = run_simulation(store, TASKS, 12, seed=7)

    assert len(attempts) == 12 * len(TASKS)
    for task in TASKS:
        # 12 次都补写过结果，样本数就是 12。
        assert store.confidence(task.category).samples == 12


def test_first_attempt_states_the_prior_when_there_is_no_history(store: FeedbackStore) -> None:
    attempts = run_simulation(store, TASKS, 1, seed=7)

    assert [attempt.expected for attempt in attempts] == [PRIOR_CONFIDENCE] * len(TASKS)


def test_same_seed_gives_the_same_result(tmp_path: Path) -> None:
    results: list[list[SimulatedAttempt]] = []
    for name in ("first.db", "second.db"):
        opened = FeedbackStore(tmp_path / name, FeedbackConfig(), ManualClock(START))
        opened.open()
        results.append(run_simulation(opened, TASKS, 20, seed=42))
        opened.close()

    assert results[0] == results[1]


def test_different_seeds_give_different_outcomes(store: FeedbackStore, tmp_path: Path) -> None:
    other = FeedbackStore(tmp_path / "other.db", FeedbackConfig(), ManualClock(START))
    other.open()
    try:
        first = run_simulation(store, TASKS, 30, seed=1)
        second = run_simulation(other, TASKS, 30, seed=2)
    finally:
        other.close()

    assert [a.succeeded for a in first] != [a.succeeded for a in second]


def test_calibration_error_is_zero_when_expectation_matches_outcomes() -> None:
    # 说 0.5 的 10 次里恰好成功 5 次：完全校准。
    attempts = [SimulatedAttempt("sim_task:x", 0.5, index % 2 == 0) for index in range(10)]

    assert expected_calibration_error(attempts) == pytest.approx(0.0)


def test_calibration_error_is_large_when_overconfident() -> None:
    # 次次说有 0.9 的把握，却一次都没成功：差 0.9。
    attempts = [SimulatedAttempt("sim_task:x", 0.9, succeeded=False) for _ in range(10)]

    assert expected_calibration_error(attempts) == pytest.approx(0.9)


def test_calibration_error_accepts_an_expectation_of_exactly_one() -> None:
    attempts = [SimulatedAttempt("sim_task:x", 1.0, succeeded=True)]

    assert expected_calibration_error(attempts) == pytest.approx(0.0)


def test_calibration_error_needs_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="没有行动"):
        expected_calibration_error([])


def test_brier_score_of_known_attempts() -> None:
    attempts = [
        SimulatedAttempt("sim_task:x", 0.8, succeeded=True),
        SimulatedAttempt("sim_task:x", 0.8, succeeded=False),
        SimulatedAttempt("sim_task:x", 0.2, succeeded=False),
    ]

    assert brier_score(attempts) == pytest.approx(0.24)


def test_brier_score_rejects_no_attempts() -> None:
    with pytest.raises(ValueError, match="没有行动"):
        brier_score([])


def test_calibration_bins_match_the_ece() -> None:
    attempts = [
        SimulatedAttempt("sim_task:x", 0.21, succeeded=True),
        SimulatedAttempt("sim_task:x", 0.24, succeeded=False),
        SimulatedAttempt("sim_task:x", 0.81, succeeded=True),
        SimulatedAttempt("sim_task:x", 1.0, succeeded=False),
    ]
    bins = calibration_bins(attempts)
    expected_error = sum(
        abs(calibration_bin.mean_expected - calibration_bin.success_rate)
        * calibration_bin.count
        / len(attempts)
        for calibration_bin in bins
    )

    assert expected_calibration_error(attempts) == pytest.approx(expected_error)


@pytest.mark.parametrize("rate", [-0.1, 1.1])
def test_simulated_task_rejects_an_impossible_success_rate(rate: float) -> None:
    with pytest.raises(ValueError, match="成功率"):
        SimulatedTask("bad", rate)
