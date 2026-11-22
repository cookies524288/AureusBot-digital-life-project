"""chat 命令的测试：假器官、临时数据根目录、假的键盘输入，不调用模型。"""

import argparse
import asyncio
import errno
import textwrap
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from aureus.cli.chat_command import EXIT_FAILURE, EXIT_OK, run_chat_command
from aureus.common import AureusPaths, shutdown_logging
from aureus.heartbeat import EVENT_MESSAGE, EVENT_SHUT_DOWN
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery

IDENTITY = textwrap.dedent(
    """\
    [facts]
    name = "测试豆"
    birthday = "2008-01-01"

    [user]
    name = "测试用户"

    [persona]
    immutable = "你是一个测试用的数字生命。"
    """
)

FAKE = argparse.Namespace(fake=True)


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    # 心跳默认 60 秒一拍；测试里调成 1 秒，免得关闭时多等。
    paths.runtime_config_file.write_text(
        "[heartbeat]\ntick_interval_seconds = 1.0\n[web]\nenabled = false\n", encoding="utf-8"
    )
    return paths


def scripted_input(lines: list[str], replied: threading.Event) -> Callable[[], str]:
    """先说话，等它回复了再敲 /quit；行用完就当作输入结束。"""
    remaining = list(lines)

    def read_line() -> str:
        if not remaining:
            raise EOFError
        line = remaining.pop(0)
        if line == "/quit":
            assert replied.wait(timeout=10), "等不到回复"
        return line

    return read_line


def run_chat(paths: AureusPaths, lines: list[str]) -> list[str]:
    output: list[str] = []
    replied = threading.Event()

    def write_line(text: str) -> None:
        output.append(text)
        if text.startswith("测试豆："):
            replied.set()

    code = run_chat_command(FAKE, paths, scripted_input(lines, replied), write_line)
    assert code == EXIT_OK
    return output


def read_timeline(paths: AureusPaths) -> list:
    timeline = TimelineDatabase(paths.database_file)
    timeline.open()
    try:
        return list(timeline.query(TimelineQuery()))
    finally:
        timeline.close()


def test_chat_replies_and_records_everything(paths: AureusPaths) -> None:
    output = run_chat(paths, ["你好呀", "/quit"])
    assert any(line.startswith("测试豆：") for line in output)
    assert output[-1] == "已关闭。"
    records = read_timeline(paths)
    messages = [record for record in records if record.event_type == EVENT_MESSAGE]
    assert [record.actor_type for record in messages] == [ActorType.USER, ActorType.ME]
    assert messages[0].content == "你好呀"
    assert records[-1].event_type == EVENT_SHUT_DOWN


def test_occupied_web_port_is_not_advertised_and_terminal_still_replies(
    paths: AureusPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.runtime_config_file.write_text(
        "[heartbeat]\ntick_interval_seconds = 1.0\n[web]\nport = 18780\n", encoding="utf-8"
    )

    async def refuse_bind(_loop: asyncio.BaseEventLoop, *_args: object, **kwargs: object) -> None:
        assert kwargs["port"] == 18780
        raise OSError(errno.EADDRINUSE, "测试端口已被占用")

    monkeypatch.setattr(asyncio.BaseEventLoop, "create_server", refuse_bind)
    output = run_chat(paths, ["你好呀", "/quit"])
    error = next(line for line in output if line.startswith("网页没有开启："))
    assert "端口 18780" in error
    assert not any(line.startswith("网页：http://") for line in output)
    assert any(line.startswith("测试豆：") for line in output)


def test_end_of_input_shuts_down_cleanly(paths: AureusPaths) -> None:
    output = run_chat(paths, [])
    assert output[-1] == "已关闭。"
    assert read_timeline(paths)[-1].event_type == EVENT_SHUT_DOWN


def test_restart_reopens_core_and_reads_next_command(paths: AureusPaths) -> None:
    lines = iter(["/restart", "/restart", "/quit"])
    output: list[str] = []
    assert run_chat_command(FAKE, paths, lambda: next(lines, ""), output.append) == EXIT_OK
    assert output.count("正在重启……") == 2
    assert sum(row.event_type == EVENT_SHUT_DOWN for row in read_timeline(paths)) == 3


def test_restart_says_how_long_it_was_down(paths: AureusPaths) -> None:
    run_chat(paths, [])
    output = run_chat(paths, [])
    woke = next(line for line in output if line.startswith("它醒来了。"))
    # 上次是 /quit 或输入结束正常关闭的，写了关闭记录，所以说的是准确的停机时长。
    assert "关闭，停了" in woke
    assert "没有留下关闭记录" not in woke


def test_missing_identity_fails_without_traceback(
    paths: AureusPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.identity_file.unlink()
    code = run_chat_command(FAKE, paths, lambda: "", lambda _text: None)
    assert code == EXIT_FAILURE
    assert "身份配置文件不存在" in capsys.readouterr().err
