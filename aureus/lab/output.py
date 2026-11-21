"""实验台输出：运行目录、配置、指标、CSV 和结果保留（D82）。"""

import csv
import json
import re
import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from aureus.lab.experiment import Experiment, MetricValue, Table

RUN_DIRECTORY_FORMAT = "%Y%m%d-%H%M%S"
KEEP_RUNS = 20
_RUN_DIRECTORY_PATTERN = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


def create_run_directory(experiments_dir: Path, name: str, now: datetime) -> Path:
    """按传入时刻创建运行目录；同一秒重跑时使用递增后缀。"""
    experiment_dir = experiments_dir / name
    experiment_dir.mkdir(parents=True, exist_ok=True)
    base_name = now.strftime(RUN_DIRECTORY_FORMAT)
    run_dir = experiment_dir / base_name
    suffix = 2
    while run_dir.exists():
        run_dir = experiment_dir / f"{base_name}-{suffix}"
        suffix += 1
    run_dir.mkdir()
    return run_dir


def write_config(run_dir: Path, experiment: Experiment, seed: int, version: str) -> None:
    """把实验名、参数、种子和软件版本写进 config.json。"""
    _write_json(
        run_dir / "config.json",
        {
            "experiment": experiment.name,
            "parameters": dict(experiment.parameters),
            "seed": seed,
            "version": version,
        },
    )


def write_metrics(run_dir: Path, metrics: Mapping[str, MetricValue]) -> None:
    """把指标写进 metrics.json。"""
    _write_json(run_dir / "metrics.json", dict(metrics))


def write_tables(run_dir: Path, tables: tuple[Table, ...]) -> None:
    """每张表写入带 UTF-8 BOM 的 CSV，便于 Excel 直接打开中文。"""
    for table in tables:
        with (run_dir / f"{table.name}.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(table.columns)
            writer.writerows(table.rows)


def prune_runs(experiment_dir: Path, keep: int = KEEP_RUNS) -> list[Path]:
    """只保留最近的符合运行目录格式的目录，返回删除的旧目录。"""
    run_directories = sorted(
        (
            path
            for path in experiment_dir.iterdir()
            if path.is_dir() and _RUN_DIRECTORY_PATTERN.fullmatch(path.name)
        ),
        key=lambda path: path.name,
    )
    removed = run_directories[:-keep] if len(run_directories) > keep else []
    for path in removed:
        shutil.rmtree(path)
    return removed


def _write_json(path: Path, values: dict[str, Any]) -> None:
    text = json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
