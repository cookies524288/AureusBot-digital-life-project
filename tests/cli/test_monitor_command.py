"""monitor 命令的测试：只绑本机、没装 Streamlit 时中文提示、端口和数据库的边界（D64）。"""

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from aureus.cli import monitor_command
from aureus.cli.main import build_parser
from aureus.cli.monitor_command import (
    DEFAULT_MONITOR_PORT,
    LOCAL_ADDRESS,
    NOT_INSTALLED_MESSAGE,
    build_command,
    run_monitor_command,
)
from aureus.common import AureusPaths


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    return paths


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor_command, "streamlit_installed", lambda: True)


class Launches:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> int:
        self.commands.append(command)
        return 0


def args(port: int = DEFAULT_MONITOR_PORT) -> argparse.Namespace:
    return argparse.Namespace(port=port)


def test_command_binds_only_the_local_address(paths: AureusPaths) -> None:
    command = build_command(paths, 8766)
    assert command[command.index("--server.address") + 1] == LOCAL_ADDRESS == "127.0.0.1"
    assert command[command.index("--server.port") + 1] == "8766"
    assert command[command.index("--browser.gatherUsageStats") + 1] == "false"
    assert command[command.index("--server.headless") + 1] == "true"


def test_command_passes_the_data_root_to_the_page(paths: AureusPaths) -> None:
    command = build_command(paths, 8766)
    assert command[-2:] == ["--home", str(paths.home)]
    assert command.index("--") < command.index("--home")


def test_launches_when_everything_is_ready(
    paths: AureusPaths, installed: None, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.database_file.write_bytes(b"")
    launch = Launches()
    lines: list[str] = []
    assert run_monitor_command(args(), paths, launch, lines.append) == 0
    assert len(launch.commands) == 1
    assert any("127.0.0.1" in line for line in lines)


def test_without_streamlit_it_explains_in_chinese(
    paths: AureusPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(monitor_command, "streamlit_installed", lambda: False)
    paths.database_file.write_bytes(b"")
    launch = Launches()
    assert run_monitor_command(args(), paths, launch, lambda line: None) == 1
    assert launch.commands == []
    assert NOT_INSTALLED_MESSAGE in capsys.readouterr().err


def test_missing_database_is_explained_without_launching(
    paths: AureusPaths, installed: None
) -> None:
    launch = Launches()
    lines: list[str] = []
    assert run_monitor_command(args(), paths, launch, lines.append) == 0
    assert launch.commands == []
    assert lines
    assert not paths.database_file.exists()  # 不为了看一眼而新建空库


@pytest.mark.parametrize("port", [80, 1023, 65536])
def test_bad_port_is_refused(
    paths: AureusPaths, installed: None, port: int, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.database_file.write_bytes(b"")
    launch = Launches()
    assert run_monitor_command(args(port), paths, launch, lambda line: None) == 1
    assert launch.commands == []
    assert "端口" in capsys.readouterr().err


def test_parser_knows_the_monitor_command() -> None:
    parsed = build_parser().parse_args(["monitor"])
    assert (parsed.command, parsed.port) == ("monitor", DEFAULT_MONITOR_PORT)
    assert build_parser().parse_args(["monitor", "--port", "9000"]).port == 9000


def test_monitor_package_does_not_import_streamlit() -> None:
    code = "import sys, aureus.monitor; sys.exit(1 if 'streamlit' in sys.modules else 0)"
    assert subprocess.call([sys.executable, "-c", code]) == 0
