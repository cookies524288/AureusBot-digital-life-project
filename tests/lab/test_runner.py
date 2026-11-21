"""实验台运行器的测试：固定时钟、假实验和隔离输出（D82）。"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.lab.experiment import Experiment, ExperimentResult, MetricValue, Table
from aureus.lab.runner import run_experiment

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
ExperimentRun = Callable[[Mapping[str, MetricValue], int, Path], Awaitable[ExperimentResult]]


async def deterministic_run(
    parameters: Mapping[str, MetricValue], seed: int, run_dir: Path
) -> ExperimentResult:
    return ExperimentResult(
        metrics={"seed": seed, "score": 0.75},
        tables=(Table("values", ("seed",), ((seed,),)),),
        charts=(),
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
async def test_runner_writes_reproducible_metrics_and_tables(tmp_path: Path) -> None:
    first = await run_experiment(make_experiment(), 17, tmp_path, ManualClock(NOW))
    second = await run_experiment(make_experiment(), 17, tmp_path, ManualClock(NOW))

    assert first.metrics == {"seed": 17, "score": 0.75}
    assert first.charts_drawn is False
    assert first.run_dir.name == "20261101-120000"
    assert second.run_dir.name == "20261101-120000-2"
    assert (first.run_dir / "config.json").is_file()
    assert (first.run_dir / "values.csv").is_file()
    assert (first.run_dir / "metrics.json").read_bytes() == (
        second.run_dir / "metrics.json"
    ).read_bytes()


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
