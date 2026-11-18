# init 命令的测试。绝不在真实的默认位置（用户目录）建文件夹：
# 要走“默认位置”的分支时，直接调 run_init_command，把默认位置换成临时目录。
import argparse
from pathlib import Path

import pytest

from aureus.cli.init_command import run_init_command
from aureus.cli.main import main
from aureus.common import AureusPaths

Capture = pytest.CaptureFixture[str]


def no_question(_prompt: str) -> str:
    raise AssertionError("不该提问")


def answer_with(text: str):
    def ask(_prompt: str) -> str:
        return text

    return ask


def closed_input(_prompt: str) -> str:
    raise EOFError


def test_init_with_explicit_home_creates_folders_without_asking(
    tmp_path: Path, capsys: Capture
) -> None:
    home = tmp_path / "home"
    assert main(["--home", str(home), "init"], ask=no_question, environ={}) == 0
    assert AureusPaths(home).config_dir.is_dir()
    assert AureusPaths(home).snapshots_dir.is_dir()
    output = capsys.readouterr().out
    assert "已建好" in output
    assert "AUREUS_HOME" not in output  # 用户自己指定的位置，不用再教用户怎么保存


def test_init_with_environment_variable_does_not_ask(tmp_path: Path) -> None:
    home = tmp_path / "from-env"
    assert main(["init"], ask=no_question, environ={"AUREUS_HOME": str(home)}) == 0
    assert AureusPaths(home).data_dir.is_dir()


def test_init_can_be_repeated_and_keeps_existing_files(tmp_path: Path, capsys: Capture) -> None:
    paths = AureusPaths(tmp_path / "home")
    assert main(["--home", str(paths.home), "init"], ask=no_question, environ={}) == 0
    paths.identity_file.write_text("# 我的设定", encoding="utf-8")
    capsys.readouterr()
    assert main(["--home", str(paths.home), "init"], ask=no_question, environ={}) == 0
    assert "已存在" in capsys.readouterr().out
    assert paths.identity_file.read_text(encoding="utf-8") == "# 我的设定"


def test_init_without_location_and_closed_input_refuses_instead_of_using_default(
    capsys: Capture,
) -> None:
    # 没法提问时不能悄悄用默认位置（C 盘）。这里还没建任何东西，所以不会碰真实目录。
    assert main(["init"], ask=closed_input, environ={}) == 1
    error = capsys.readouterr().err
    assert "--home" in error
    assert "--yes" in error


def test_init_asks_and_uses_the_location_the_user_types(tmp_path: Path, capsys: Capture) -> None:
    default = AureusPaths(tmp_path / "default")
    chosen = tmp_path / "bigdisk" / "aureus"
    args = argparse.Namespace(yes=False)
    code = run_init_command(args, default, home_is_default=True, ask=answer_with(str(chosen)))
    assert code == 0
    assert AureusPaths(chosen).config_dir.is_dir()
    assert not default.home.exists()
    output = capsys.readouterr().out
    assert "AUREUS_HOME" in output  # 选了别处，要告诉他怎么让软件以后找到它
    assert "--home" in output


def test_init_blank_answer_uses_default_location(tmp_path: Path) -> None:
    default = AureusPaths(tmp_path / "default")
    args = argparse.Namespace(yes=False)
    assert run_init_command(args, default, home_is_default=True, ask=answer_with("  ")) == 0
    assert default.config_dir.is_dir()


def test_init_yes_accepts_default_without_asking(tmp_path: Path) -> None:
    default = AureusPaths(tmp_path / "default")
    args = argparse.Namespace(yes=True)
    assert run_init_command(args, default, home_is_default=True, ask=no_question) == 0
    assert default.config_dir.is_dir()


def test_init_rejects_relative_answer_and_creates_nothing(
    tmp_path: Path, capsys: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    default = AureusPaths(tmp_path / "default")
    args = argparse.Namespace(yes=False)
    code = run_init_command(args, default, home_is_default=True, ask=answer_with("relative/dir"))
    assert code == 1
    assert "绝对路径" in capsys.readouterr().err
    assert not default.home.exists()
    assert not (tmp_path / "relative").exists()


def test_init_reports_a_file_in_the_way(tmp_path: Path, capsys: Capture) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("我是个文件", encoding="utf-8")
    assert main(["--home", str(blocker), "init"], ask=no_question, environ={}) == 1
    assert "建不了" in capsys.readouterr().err
