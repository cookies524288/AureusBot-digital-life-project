"""实验台运行器的测试：固定时钟、假实验和隔离输出（D82）。"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import AureusPaths, ManualClock
from aureus.lab import runner
from aureus.lab.calibration import CALIBRATION_EXPERIMENT
from aureus.lab.energy import ENERGY_EXPERIMENT
from aureus.lab.experiment import Chart, Experiment, ExperimentResult, MetricValue, Series, Table
from aureus.lab.runner import run_experiment

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
ExperimentRun = Callable[[Mapping[str, MetricValue], int, Path], Awaitable[ExperimentResult]]


async def deterministic_run(
    parameters: Mapping[str, MetricValue], seed: int, run_dir: Path
) -> ExperimentResult:
    return ExperimentResult(
        metrics={"seed": seed, "score": 0.75},
        tables=(Table("values", ("seed",), ((seed,),)),),
        charts=(
            Chart("score", "Score", "Seed", "Score", (Series("Score", (float(seed),), (0.75,)),)),
        ),
    )


def make_experiment(run: ExperimentRun = deterministic_run) -> Experiment:
    return Experiment(
        name="sample",
        code="E0",
        title="示例实验",
        parameters={},
        run=run,
    )


@pytest.mark.asyncio
async def test_runner_writes_reproducible_metrics_and_tables_without_matplotlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "matplotlib_installed", lambda: False)
    first = await run_experiment(make_experiment(), 17, tmp_path, ManualClock(NOW))
    second = await run_experiment(make_experiment(), 17, tmp_path, ManualClock(NOW))

    assert first.metrics == {"seed": 17, "score": 0.75}
    assert first.charts_drawn is False
    assert first.run_dir.name == "20261101-120000"
    assert second.run_dir.name == "20261101-120000-2"
    assert (first.run_dir / "config.json").is_file()
    assert (first.run_dir / "values.csv").is_file()
    assert not list(first.run_dir.glob("*.png"))
    assert (first.run_dir / "metrics.json").read_bytes() == (
        second.run_dir / "metrics.json"
    ).read_bytes()


@pytest.mark.asyncio
async def test_energy_metrics_repeat_with_the_same_seed_and_change_with_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "matplotlib_installed", lambda: False)
    parameters = {**ENERGY_EXPERIMENT.parameters, "days": 4, "optional_wishes_per_day": 4}
    experiment = replace(ENERGY_EXPERIMENT, parameters=parameters)
    clock = ManualClock(NOW)

    paths = AureusPaths(tmp_path)
    first = await run_experiment(experiment, 0, paths.experiments_dir, clock)
    same_seed = await run_experiment(experiment, 0, paths.experiments_dir, clock)
    different_seed = await run_experiment(experiment, 1, paths.experiments_dir, clock)

    metrics = (first.run_dir / "metrics.json").read_bytes()
    assert metrics == (same_seed.run_dir / "metrics.json").read_bytes()
    assert metrics != (different_seed.run_dir / "metrics.json").read_bytes()
    assert set(tmp_path.iterdir()) == {paths.data_dir}
    assert set(paths.data_dir.iterdir()) == {paths.experiments_dir}


@pytest.mark.asyncio
async def test_runner_draws_calibration_pngs(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    report = await run_experiment(CALIBRATION_EXPERIMENT, 0, tmp_path, ManualClock(NOW))

    assert report.charts_drawn is True
    assert set(path.name for path in report.run_dir.iterdir()) == {
        "config.json",
        "metrics.json",
        "convergence.csv",
        "reliability.csv",
        "ece_by_round.png",
        "reliability.png",
        "experiment.db",
    }
    for path in report.run_dir.glob("*.png"):
        png = path.read_bytes()
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 8


@pytest.mark.asyncio
async def test_runner_leaves_metrics_and_tables_when_plotting_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(run_dir: Path, charts: tuple[Chart, ...]) -> list[Path]:
        raise RuntimeError("画图失败")

    monkeypatch.setattr(runner, "matplotlib_installed", lambda: True)
    monkeypatch.setattr(runner, "draw_charts", fail)

    with pytest.raises(RuntimeError, match="画图失败"):
        await run_experiment(make_experiment(), 3, tmp_path, ManualClock(NOW))

    run_dir = tmp_path / "sample" / "20261101-120000"
    assert (run_dir / "metrics.json").is_file()
    assert (run_dir / "values.csv").is_file()


@pytest.mark.asyncio
async def test_runner_keeps_config_and_restores_log_level_after_failure(tmp_path: Path) -> None:
    async def fail(
        parameters: Mapping[str, MetricValue], seed: int, run_dir: Path
    ) -> ExperimentResult:
        assert logging.getLogger("aureus").getEffectiveLevel() == logging.ERROR
        raise RuntimeError("模拟失败")

    aureus_logger = logging.getLogger("aureus")
    old_level = aureus_logger.level
    aureus_logger.setLevel(logging.INFO)
    old_run = tmp_path / "sample" / "20261031-000000"
    old_run.mkdir(parents=True)
    try:
        with pytest.raises(RuntimeError, match="模拟失败"):
            await run_experiment(make_experiment(fail), 3, tmp_path, ManualClock(NOW))

        failed_run = tmp_path / "sample" / "20261101-120000"
        assert (failed_run / "config.json").is_file()
        assert not (failed_run / "metrics.json").exists()
        assert old_run.is_dir()
        assert aureus_logger.level == logging.INFO
    finally:
        aureus_logger.setLevel(old_level)
