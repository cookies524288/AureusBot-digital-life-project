"""实验台 runner：创建运行目录、执行实验并保存结果（D82）。"""

import importlib.metadata
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from aureus.common import Clock
from aureus.lab.charts import draw_charts, matplotlib_installed
from aureus.lab.experiment import Experiment, MetricValue
from aureus.lab.output import (
    create_run_directory,
    prune_runs,
    write_config,
    write_metrics,
    write_tables,
)


@dataclass(frozen=True)
class RunReport:
    """一次实验运行的结果位置和输出状态。"""

    run_dir: Path
    metrics: Mapping[str, MetricValue]
    charts_drawn: bool
    pruned: int


async def run_experiment(
    experiment: Experiment, seed: int, experiments_dir: Path, clock: Clock
) -> RunReport:
    """执行一个实验并将配置、指标和表格写到独立运行目录。

    Raises:
        Exception: 实验执行或结果写入失败时原样抛出，运行目录保留供检查。
    """
    run_dir = create_run_directory(experiments_dir, experiment.name, clock.now())
    try:
        return await _run_in_directory(experiment, seed, run_dir)
    except Exception as error:
        # 让命令能报告已创建的目录，异常的类型和正文照原样传回。
        error.add_note(f"运行目录：{run_dir}")
        raise


async def _run_in_directory(experiment: Experiment, seed: int, run_dir: Path) -> RunReport:
    write_config(run_dir, experiment, seed, importlib.metadata.version("aureus"))
    aureus_logger = logging.getLogger("aureus")
    previous_level = aureus_logger.level
    aureus_logger.setLevel(logging.ERROR)
    try:
        result = await experiment.run(experiment.parameters, seed, run_dir)
    finally:
        aureus_logger.setLevel(previous_level)
    write_metrics(run_dir, result.metrics)
    write_tables(run_dir, result.tables)
    charts_drawn = matplotlib_installed()
    if charts_drawn:
        draw_charts(run_dir, result.charts)
    pruned = prune_runs(run_dir.parent)
    return RunReport(
        run_dir=run_dir,
        metrics=dict(result.metrics),
        charts_drawn=charts_drawn,
        pruned=len(pruned),
    )
