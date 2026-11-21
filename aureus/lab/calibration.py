"""E4 置信度校准：比较反馈自信和固定先验的误差（D82）。"""

from collections.abc import Mapping
from pathlib import Path

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
from aureus.lab.experiment import (
    LAB_START,
    Chart,
    Experiment,
    ExperimentResult,
    MetricValue,
    Series,
    Table,
)

CALIBRATION_PARAMETERS: Mapping[str, MetricValue] = {
    "rate_hard": 0.3,
    "rate_mid": 0.7,
    "rate_easy": 0.9,
    "rounds": 200,
    "window_rounds": 20,
}


def _tasks(parameters: Mapping[str, MetricValue]) -> tuple[SimulatedTask, ...]:
    return (
        SimulatedTask("hard", float(parameters["rate_hard"])),
        SimulatedTask("mid", float(parameters["rate_mid"])),
        SimulatedTask("easy", float(parameters["rate_easy"])),
    )


async def run_calibration(
    parameters: Mapping[str, MetricValue], seed: int, run_dir: Path
) -> ExperimentResult:
    """运行 E4，并返回反馈组和先验对照组的指标、表格及折线图数据。"""
    rounds = int(parameters["rounds"])
    window_rounds = int(parameters["window_rounds"])
    tasks = _tasks(parameters)
    store = FeedbackStore(run_dir / "experiment.db", FeedbackConfig(), ManualClock(LAB_START))
    store.open()
    try:
        attempts = run_simulation(store, tasks, rounds, seed)
    finally:
        store.close()

    prior_attempts = tuple(
        SimulatedAttempt(attempt.category, PRIOR_CONFIDENCE, attempt.succeeded)
        for attempt in attempts
    )
    metrics: dict[str, MetricValue] = {
        "feedback.ece": expected_calibration_error(attempts),
        "feedback.brier": brier_score(attempts),
        "prior.ece": expected_calibration_error(prior_attempts),
        "prior.brier": brier_score(prior_attempts),
        "attempts": len(attempts),
    }
    convergence = _convergence_rows(attempts, rounds, window_rounds, len(tasks))
    bins = calibration_bins(attempts)
    reliability = Table(
        name="reliability",
        columns=("lower", "upper", "count", "mean_expected", "success_rate"),
        rows=tuple(
            (item.lower, item.upper, item.count, item.mean_expected, item.success_rate)
            for item in bins
        ),
    )
    charts = (
        Chart(
            name="ece_by_round",
            title="反馈自信的校准误差",
            x_label="轮数",
            y_label="ECE",
            series=(
                Series(
                    label="反馈",
                    x=tuple(float(row[0]) for row in convergence),
                    y=tuple(float(row[1]) for row in convergence),
                ),
                Series(
                    label="先验对照",
                    x=tuple(float(row[0]) for row in convergence),
                    y=tuple(float(row[2]) for row in convergence),
                ),
            ),
        ),
        Chart(
            name="reliability",
            title="置信度可靠性",
            x_label="说的把握",
            y_label="实际成功率",
            series=(
                Series(label="理想", x=(0.0, 1.0), y=(0.0, 1.0)),
                Series(
                    label="反馈",
                    x=tuple(item.mean_expected for item in bins),
                    y=tuple(item.success_rate for item in bins),
                ),
            ),
        ),
    )
    return ExperimentResult(
        metrics=metrics,
        tables=(
            Table(
                name="convergence",
                columns=("round_end", "feedback_ece", "prior_ece"),
                rows=convergence,
            ),
            reliability,
        ),
        charts=charts,
    )


def _convergence_rows(
    attempts: list[SimulatedAttempt], rounds: int, window_rounds: int, tasks_per_round: int
) -> tuple[tuple[MetricValue, ...], ...]:
    rows: list[tuple[MetricValue, ...]] = []
    window_size = window_rounds * tasks_per_round
    for start in range(0, len(attempts), window_size):
        end = min(start + window_size, len(attempts))
        feedback_window = attempts[start:end]
        prior_window = tuple(
            SimulatedAttempt(attempt.category, PRIOR_CONFIDENCE, attempt.succeeded)
            for attempt in feedback_window
        )
        round_end = min((start // tasks_per_round) + window_rounds, rounds)
        rows.append(
            (
                round_end,
                expected_calibration_error(feedback_window),
                expected_calibration_error(prior_window),
            )
        )
    return tuple(rows)


CALIBRATION_EXPERIMENT = Experiment(
    name="calibration",
    code="E4",
    title="置信度校准：反馈得来的自信准不准",
    parameters=CALIBRATION_PARAMETERS,
    run=run_calibration,
)
