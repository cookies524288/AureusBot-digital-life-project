"""实验台命令的参数、输出和结果保留测试（D82）。"""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from aureus.cli import lab_command
from aureus.cli.main import main
from aureus.common import AureusPaths, ManualClock
from aureus.lab import EXPERIMENTS, LAB_START, ExperimentResult, MetricValue, runner


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    clock = ManualClock(LAB_START)
    monkeypatch.setattr(lab_command, "SystemClock", lambda: clock)
    return clock


def test_lab_list_prints_the_two_registered_experiments(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lab", "list"], environ={}) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("calibration  E4  ")
    assert lines[1].startswith("energy  E2  ")


def test_lab_runs_calibration_and_writes_charts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("matplotlib")
    image = pytest.importorskip("PIL.Image")
    paths = AureusPaths(tmp_path)

    assert main(["lab", "run", "calibration", "--home", str(paths.home)], environ={}) == 0

    run_dir = paths.experiments_dir / "calibration" / "20261101-000000"
    output = capsys.readouterr().out
    assert f"运行目录：{run_dir}" in output
    assert "已画图 2 张" in output
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["attempts"] == 600
    assert metrics["feedback.ece"] < metrics["prior.ece"]
    assert metrics["feedback.brier"] < metrics["prior.brier"]
    charts = sorted(run_dir.glob("*.png"))
    assert [path.name for path in charts] == ["ece_by_round.png", "reliability.png"]
    for path in charts:
        with image.open(path) as chart:
            assert chart.format == "PNG"
            assert chart.convert("RGB").getcolors(2) is None


def test_lab_runs_calibration_without_matplotlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner, "matplotlib_installed", lambda: False)
    paths = AureusPaths(tmp_path / "new-home")

    assert main(["lab", "run", "calibration", "--home", str(paths.home)], environ={}) == 0

    run_dir = paths.experiments_dir / "calibration" / "20261101-000000"
    output = capsys.readouterr().out
    assert f"运行目录：{run_dir}" in output
    assert "feedback.ece：0.0495" in output
    assert "uv sync --extra lab" in output
    assert set(path.name for path in run_dir.iterdir()) == {
        "config.json",
        "metrics.json",
        "convergence.csv",
        "reliability.csv",
        "experiment.db",
    }


def test_lab_rejects_an_unknown_experiment_and_shows_choices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["lab", "run", "unknown", "--home", str(tmp_path)], environ={}) == 1
    assert "没有叫 unknown 的实验，可用的有：calibration、energy" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_lab_rejects_a_negative_seed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["lab", "run", "calibration", "--seed", "-1"], environ={})
    assert error.value.code == 2
    assert "必须是 0 或正整数" in capsys.readouterr().err


def test_lab_reports_a_failed_run_with_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail(
        parameters: Mapping[str, MetricValue], seed: int, run_dir: Path
    ) -> ExperimentResult:
        raise RuntimeError("模拟故障")

    experiment = replace(EXPERIMENTS["calibration"], run=fail)
    monkeypatch.setattr(lab_command, "EXPERIMENTS", {experiment.name: experiment})
    paths = AureusPaths(tmp_path)

    assert main(["lab", "run", "calibration", "--home", str(tmp_path)], environ={}) == 1

    run_dir = paths.experiments_dir / "calibration" / "20261101-000000"
    assert capsys.readouterr().err.strip() == f"实验失败：模拟故障，运行目录：{run_dir}"
    assert (run_dir / "config.json").is_file()


def test_lab_keeps_only_twenty_runs_and_reports_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: ManualClock,
) -> None:
    async def finish(
        parameters: Mapping[str, MetricValue], seed: int, run_dir: Path
    ) -> ExperimentResult:
        return ExperimentResult(metrics={}, tables=(), charts=())

    experiment = replace(EXPERIMENTS["calibration"], run=finish)
    monkeypatch.setattr(lab_command, "EXPERIMENTS", {experiment.name: experiment})
    monkeypatch.setattr(runner, "matplotlib_installed", lambda: False)
    experiment_dir = AureusPaths(tmp_path).experiments_dir / "calibration"
    notes = experiment_dir / "notes"
    notes.mkdir(parents=True)
    for _ in range(21):
        assert main(["--home", str(tmp_path), "lab", "run", "calibration"], environ={}) == 0
        clock.advance(timedelta(seconds=1))

    assert len([path for path in experiment_dir.iterdir() if path != notes]) == 20
    assert not (experiment_dir / "20261101-000000").exists()
    assert notes.is_dir()
    assert capsys.readouterr().out.count("删掉了 1 个更早的运行结果") == 1
