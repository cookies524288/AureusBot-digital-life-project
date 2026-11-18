"""终端通道的测试：用假的输入输出，不碰真的键盘。"""

import asyncio
import threading
from collections.abc import Iterator

import pytest

from aureus.channels import CHANNEL_NAME, TERMINAL_USER_ID, TerminalChannel
from aureus.heartbeat import MessageKind, OutgoingMessage, Perception, TrustLevel, VisibilityScope


class FakeHeartbeat:
    def __init__(self, accepting: bool = True) -> None:
        self.accepting = accepting
        self.received: list[Perception] = []
        self.stop_requests = 0
        self.thread_ids: list[int] = []

    def submit(self, perception: Perception) -> bool:
        self.thread_ids.append(threading.get_ident())
        self.received.append(perception)
        return self.accepting

    def describe_state(self) -> str:
        return "我现在待机中。"

    def request_stop(self) -> None:
        self.thread_ids.append(threading.get_ident())
        self.stop_requests += 1


def scripted_input(*lines: str, end: BaseException | None = None) -> Iterator[str]:
    yield from lines
    raise end or EOFError


class Terminal:
    def __init__(self, *lines: str, accepting: bool = True, end: BaseException | None = None):
        self.heartbeat = FakeHeartbeat(accepting)
        self.output: list[str] = []
        source = scripted_input(*lines, end=end)
        self.channel = TerminalChannel(
            self.heartbeat, read_line=lambda: next(source), write_line=self.output.append
        )


def test_text_becomes_a_verified_private_perception() -> None:
    terminal = Terminal()

    terminal.channel.handle_line("  今天面试被拒了  ")

    assert terminal.heartbeat.received == [
        Perception(
            channel=CHANNEL_NAME,
            sender_id=TERMINAL_USER_ID,
            content="今天面试被拒了",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    ]
    assert terminal.output == []


@pytest.mark.parametrize("line", ["", "   ", "\t"])
def test_blank_lines_are_ignored(line: str) -> None:
    terminal = Terminal()

    terminal.channel.handle_line(line)

    assert terminal.heartbeat.received == []
    assert terminal.output == []


def test_state_command_prints_the_heartbeat_state() -> None:
    terminal = Terminal()

    terminal.channel.handle_line("/STATE")

    assert terminal.output == ["我现在待机中。"]
    assert terminal.heartbeat.received == []


@pytest.mark.parametrize("command", ["/quit", "/exit", " /Quit "])
def test_quit_commands_request_a_stop(command: str) -> None:
    terminal = Terminal()

    terminal.channel.handle_line(command)

    assert terminal.heartbeat.stop_requests == 1


def test_help_and_unknown_commands() -> None:
    terminal = Terminal()

    terminal.channel.handle_line("/help")
    terminal.channel.handle_line("/dance")

    assert "/state" in terminal.output[0]
    assert "不认识命令 /dance" in terminal.output[1]
    assert terminal.heartbeat.received == []


def test_text_refused_during_shutdown_is_reported() -> None:
    terminal = Terminal(accepting=False)

    terminal.channel.handle_line("还在吗")

    assert terminal.output == ["正在关闭，这句没有收到。"]


def test_replies_and_notices_look_different() -> None:
    terminal = Terminal()

    terminal.channel.show(OutgoingMessage("terminal", "local", "早呀。", MessageKind.REPLY, "小绿"))
    terminal.channel.show(
        OutgoingMessage("terminal", "local", "预算用完了", MessageKind.NOTICE, "系统")
    )

    assert terminal.output == ["小绿：早呀。", "【系统】预算用完了"]


async def wait_until(condition: object, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():  # type: ignore[operator]
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("end", [EOFError(), KeyboardInterrupt(), OSError("console gone")])
async def test_reader_thread_hands_lines_to_the_event_loop_then_stops(end: BaseException) -> None:
    terminal = Terminal("你好", "/state", end=end)
    loop_thread = threading.get_ident()

    terminal.channel.start()
    await wait_until(lambda: terminal.heartbeat.stop_requests == 1)

    assert [p.content for p in terminal.heartbeat.received] == ["你好"]
    assert "我现在待机中。" in terminal.output
    # 心跳只在事件循环的线程里被调用，读键盘的线程不直接碰它。
    assert set(terminal.heartbeat.thread_ids) == {loop_thread}


@pytest.mark.asyncio
async def test_starting_twice_is_a_programming_error() -> None:
    terminal = Terminal()
    terminal.channel.start()

    with pytest.raises(RuntimeError, match="已经开始"):
        terminal.channel.start()
    await wait_until(lambda: terminal.heartbeat.stop_requests == 1)


def test_end_of_input_after_quit_does_not_announce_twice() -> None:
    terminal = Terminal()

    terminal.channel.handle_line("/quit")
    terminal.channel.handle_end_of_input()

    assert terminal.output == ["正在关闭……"]
    assert terminal.heartbeat.stop_requests == 2  # 再请一次也无妨，心跳那边是幂等的
