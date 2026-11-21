"""实验台文件输出和运行结果保留的测试（D82）。"""

import csv
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from aureus.common import AureusPaths
from aureus.lab.experiment import Experiment, ExperimentResult, MetricValue, Table
from aureus.lab.output import (
    KEEP_RUNS,
    create_run_directory,
    prune_runs,
    write_config,
    write_metrics,
    write_tables,
)


async def empty_experiment_run(
    parameters: Mapping[str, MetricValue], seed: int, run_dir: Path
) -> ExperimentResult:
    return ExperimentResult(metrics={}, tables=(), charts=())


def test_create_run_directory_uses_the_clock_value_and_adds_a_suffix(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, 34, 56, tzinfo=UTC)
    first = create_run_directory(tmp_path, "sample", now)
    second = create_run_directory(tmp_path, "sample", now)

    assert first.name == "20261101-123456"
    assert second.name == "20261101-123456-2"


def test_config_metrics_and_tables_use_the_required_formats(tmp_path: Path) -> None:
    experiment = Experiment(
        name="sample",
        code="E0",
        title="示例实验",
        parameters={"次数": 3},
        run=empty_experiment_run,
    )
    run_dir = create_run_directory(
        tmp_path, "sample", datetime(2026, 11, 1, 12, 34, 56, tzinfo=UTC)
    )
    write_config(run_dir, experiment, seed=7, version="0.0.1")
    write_metrics(run_dir, {"准确率": 0.75, "次数": 3})
    write_tables(run_dir, (Table("scores", ("name", "score"), (("甲", 0.75),)),))

    config_text = (run_dir / "config.json").read_text(encoding="utf-8")
    assert (
        config_text
        == json.dumps(
            {
                "experiment": "sample",
                "parameters": {"次数": 3},
                "seed": 7,
                "version": "0.0.1",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    metrics_text = (run_dir / "metrics.json").read_text(encoding="utf-8")
    assert (
        metrics_text
        == json.dumps({"准确率": 0.75, "次数": 3}, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    csv_path = run_dir / "scores.csv"
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
        assert list(csv.reader(csv_file)) == [["name", "score"], ["甲", "0.75"]]


def test_prune_runs_keeps_the_latest_twenty_and_ignores_other_entries(tmp_path: Path) -> None:
    for day in range(1, KEEP_RUNS + 3):
        (tmp_path / f"202501{day:02d}-000000").mkdir()
    manual_directory = tmp_path / "manual"
    manual_directory.mkdir()
    manual_file = tmp_path / "notes.txt"
    manual_file.write_text("保留", encoding="utf-8")

    removed = prune_runs(tmp_path)

    run_directories = [
        path for path in tmp_path.iterdir() if path.is_dir() and path.name.startswith("2025")
    ]
    assert [path.name for path in removed] == ["20250101-000000", "20250102-000000"]
    assert len(run_directories) == KEEP_RUNS
    assert manual_directory.is_dir()
    assert manual_file.read_text(encoding="utf-8") == "保留"


def test_paths_expose_experiments_without_creating_it_during_init(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path / "home")

    assert paths.experiments_dir == paths.data_dir / "experiments"
    paths.ensure_directories()
    assert not paths.experiments_dir.exists()
