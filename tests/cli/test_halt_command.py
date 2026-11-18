# halt 和 resume 命令的测试：走命令行入口 main，临时数据根目录。
from pathlib import Path

import pytest

from aureus.cli.main import main
from aureus.common import AureusPaths, ManualClock
from aureus.safety import EmergencyStop

Capture = pytest.CaptureFixture[str]


def stop_of(paths: AureusPaths) -> EmergencyStop:
    from datetime import UTC, datetime

    return EmergencyStop(paths.halt_file, ManualClock(datetime(2026, 11, 18, tzinfo=UTC)))


def test_halt_then_resume(tmp_path: Path, capsys: Capture) -> None:
    paths = AureusPaths(tmp_path / "home")

    assert main(["--home", str(paths.home), "halt", "要出门"]) == 0
    assert "已急停" in capsys.readouterr().out
    assert stop_of(paths).status().halted is True
    assert stop_of(paths).status().reason == "要出门"

    assert main(["--home", str(paths.home), "resume"]) == 0
    assert "已解除" in capsys.readouterr().out
    assert stop_of(paths).status().halted is False


def test_halting_twice_and_resuming_twice_are_harmless(tmp_path: Path, capsys: Capture) -> None:
    home = str(tmp_path / "home")
    assert main(["--home", home, "halt"]) == 0
    assert main(["--home", home, "halt"]) == 0
    assert "本来就在急停中" in capsys.readouterr().out
    assert main(["--home", home, "resume"]) == 0
    assert main(["--home", home, "resume"]) == 0
    assert "本来就没有急停" in capsys.readouterr().out


def test_halt_reports_failure_when_the_marker_cannot_be_written(
    tmp_path: Path, capsys: Capture
) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.halt_file.parent.mkdir(parents=True)
    paths.halt_file.with_name("halt.json.tmp").mkdir()  # 临时文件的位置被文件夹占了
    assert main(["--home", str(paths.home), "halt"]) == 1
    assert "没有生效" in capsys.readouterr().err
