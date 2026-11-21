"""E4 反馈自信校准的指标、收敛表和边界轮数测试（D82）。"""

from math import ceil
from pathlib import Path

import pytest

from aureus.lab.calibration import CALIBRATION_EXPERIMENT, run_calibration
from aureus.lab.experiment import MetricValue


@pytest.mark.asyncio
async def test_feedback_ece_and_brier_are_below_the_prior(tmp_path: Path) -> None:
    result = await run_calibration(CALIBRATION_EXPERIMENT.parameters, 0, tmp_path)

    assert float(result.metrics["feedback.ece"]) < float(result.metrics["prior.ece"])
    assert float(result.metrics["feedback.brier"]) < float(result.metrics["prior.brier"])
    assert result.metrics["attempts"] == 600
    assert (tmp_path / "experiment.db").is_file()


@pytest.mark.asyncio
async def test_convergence_uses_independent_windows_and_improves(tmp_path: Path) -> None:
    result = await run_calibration(CALIBRATION_EXPERIMENT.parameters, 0, tmp_path)
    convergence = result.tables[0]
    feedback_errors = [float(row[1]) for row in convergence.rows]

    assert len(convergence.rows) == ceil(200 / 20)
    assert feedback_errors[-1] < feedback_errors[0]
    assert convergence.rows[0][0] == 20
    assert convergence.rows[-1][0] == 200


@pytest.mark.asyncio
async def test_convergence_includes_a_partial_last_window(tmp_path: Path) -> None:
    parameters: dict[str, MetricValue] = dict(CALIBRATION_EXPERIMENT.parameters)
    parameters["rounds"] = 23
    parameters["window_rounds"] = 10

    result = await run_calibration(parameters, 0, tmp_path)

    assert len(result.tables[0].rows) == ceil(23 / 10)
    assert result.tables[0].rows[-1][0] == 23
    assert result.metrics["attempts"] == 69
    assert len(result.tables[1].rows) > 0
    assert [chart.name for chart in result.charts] == ["ece_by_round", "reliability"]
