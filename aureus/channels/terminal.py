"""终端通道 terminal：在命令行里和它聊天（03 号文档“通道”一节，D41）。

通道只做翻译：把敲进来的一行字变成感知交给心跳，把心跳交回的消息打印出来。
不做决定，不存状态。

读键盘会一直卡住，所以放在单独的后台线程里读，读到一行再交回事件循环处理，
心跳不会被它卡住。这个线程是守护线程：程序退出时不用等它（它可能正卡在等输入）。
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Protocol

from aureus.heartbeat import (
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
)

logger = logging.getLogger(__name__)

CHANNEL_NAME = "terminal"
# 终端在本机运行，坐在键盘前的就是用户本人（identity.example.toml：终端默认就是你）。
TERMINAL_USER_ID = "local"

COMMAND_HELP = "/help"
COMMAND_STATE = "/state"
COMMAND_HALT = "/halt"
COMMAND_RESUME = "/resume"
COMMAND_QUIT = "/quit"
QUIT_COMMANDS = frozenset({COMMAND_QUIT, "/exit"})

HELP_TEXT = (
    "直接输入文字就是和它说话。命令：\n"
    f"  {COMMAND_STATE}  看它此刻的状态\n"
    f"  {COMMAND_HALT} [原因]  急停：它不再调用模型、不再回复，消息照常记录\n"
    f"  {COMMAND_RESUME}  解除急停\n"
    f"  {COMMAND_QUIT}   关闭 Aureus\n"
    f"  {COMMAND_HELP}   显示这段说明"
)
STOP_SHORTCUTS = "\n  Ctrl+C 或结束输入  关闭 Aureus"
WINDOW_SHORTCUTS = "\n  Ctrl+C 或关闭窗口  只关闭 CLI 窗口"

ReadLine = Callable[[], str]
WriteLine = Callable[[str], None]


class HeartbeatHandle(Protocol):
    """终端通道用到的心跳接口。"""

    def submit(self, perception: Perception) -> bool:
        """交一条感知给心跳。"""
        ...

    def describe_state(self) -> str:
        """此刻的状态，给人看。"""
        ...

    def request_stop(self) -> None:
        """请心跳停下来。"""
        ...

    def halt(self, reason: str = "") -> bool:
        """设急停；本来就在急停中返回 False。写不进标记文件时抛 OSError。"""
        ...

    def resume(self) -> bool:
        """解除急停；本来就不在急停中返回 False。删不掉标记文件时抛 OSError。"""
        ...


class TerminalChannel:
    """终端通道。先 attach 到心跳上，再在事件循环里 start()。"""

    def __init__(
        self,
        heartbeat: HeartbeatHandle,
        read_line: ReadLine = input,
        write_line: WriteLine = print,
        *,
        close_on_interrupt: bool = True,
    ) -> None:
        """创建终端通道。

        Args:
            heartbeat: 心跳。
            read_line: 读一行输入的函数，读到结尾时抛 EOFError；默认是 input，测试时传假的。
            write_line: 打印一行的函数，默认是 print，测试时传假的。
            close_on_interrupt: 结束输入或 Ctrl+C 是否关闭 Aureus。
        """
        self._heartbeat = heartbeat
        self._read_line = read_line
        self._write_line = write_line
        self._close_on_interrupt = close_on_interrupt
        self._reader: threading.Thread | None = None
        # 已经请心跳停下来了：之后输入结束时不再重复提示。
        self._stop_requested = False

    def show(self, message: OutgoingMessage) -> None:
        """心跳交回的消息：打印出来。这个函数要交给 Heartbeat.attach_channel。"""
        if message.kind is MessageKind.NOTICE:
            self._write_line(f"【{message.sender_name}】{message.content}")
        else:
            self._write_line(f"{message.sender_name}：{message.content}")

    def handle_line(self, line: str) -> None:
        """处理敲进来的一行：命令就执行，其余的当成对它说的话。在事件循环里调用。"""
        text = line.strip()
        if not text:
            return
        command = text.lower()
        if command == COMMAND_HELP:
            self._write_line(self._help_text())
        elif command == COMMAND_STATE:
            self._write_line(self._heartbeat.describe_state())
        elif command == COMMAND_HALT or command.startswith(f"{COMMAND_HALT} "):
            self._halt(text[len(COMMAND_HALT) :].strip())
        elif command == COMMAND_RESUME:
            self._resume()
        elif command in QUIT_COMMANDS:
            self._request_stop("正在关闭……")
        elif command.startswith("/"):
            self._write_line(f"不认识命令 {text}。输入 {COMMAND_HELP} 看有哪些命令。")
        else:
            accepted = self._heartbeat.submit(
                Perception(
                    channel=CHANNEL_NAME,
                    sender_id=TERMINAL_USER_ID,
                    content=text,
                    visibility=VisibilityScope.USER_ONLY,
                    trust=TrustLevel.VERIFIED_USER,
                )
            )
            if not accepted:
                self._write_line("正在关闭，这句没有收到。")

    def _halt(self, reason: str) -> None:
        try:
            engaged = self._heartbeat.halt(reason)
        except OSError as error:
            # 没写成功就要说清楚：用户会以为它已经停了。
            self._write_line(f"急停没有生效：写不进急停标记（{error}）。")
            return
        self._write_line(
            "已急停。它不会再调用模型、不会再回复，收到的消息照常记录。"
            if engaged
            else "本来就在急停中。"
        )

    def _resume(self) -> None:
        try:
            released = self._heartbeat.resume()
        except OSError as error:
            self._write_line(f"解除失败：删不掉急停标记（{error}），它还在急停中。")
            return
        self._write_line("已解除急停。" if released else "本来就没有急停。")

    def handle_end_of_input(self) -> None:
        """输入结束（Ctrl+D、Windows 上的 Ctrl+Z 回车，或者输入被关掉）：请心跳停下来。"""
        if self._close_on_interrupt:
            self._request_stop("输入结束，正在关闭……")

    def _help_text(self) -> str:
        return HELP_TEXT + (STOP_SHORTCUTS if self._close_on_interrupt else WINDOW_SHORTCUTS)

    def _request_stop(self, announcement: str) -> None:
        """请心跳停下来；只提示一次。"""
        if not self._stop_requested:
            self._stop_requested = True
            self._write_line(announcement)
        self._heartbeat.request_stop()

    def start(self) -> None:
        """开始在后台线程里读键盘。要在事件循环里调用。

        Raises:
            RuntimeError: 已经开始过了。
        """
        if self._reader is not None:
            raise RuntimeError("终端通道已经开始读输入了")
        loop = asyncio.get_running_loop()
        self._write_line(f"已连上。{self._help_text()}")
        self._reader = threading.Thread(
            target=self._read_forever, args=(loop,), name="aureus-terminal-input", daemon=True
        )
        self._reader.start()

    def _read_forever(self, loop: asyncio.AbstractEventLoop) -> None:
        """后台线程：一行一行地读，交回事件循环处理。这里不碰心跳，只转交。"""
        while True:
            try:
                line = self._read_line()
            except (EOFError, KeyboardInterrupt):
                # Windows 上按 Ctrl+C 时，正在等输入的线程可能会收到这两种之一。
                self._call_in_loop(loop, self.handle_end_of_input)
                return
            except Exception:
                logger.exception("读终端输入时出错，不再读取")
                self._call_in_loop(loop, self.handle_end_of_input)
                return
            if not self._call_in_loop(loop, self.handle_line, line):
                return

    @staticmethod
    def _call_in_loop(
        loop: asyncio.AbstractEventLoop, function: Callable[..., None], *args: str
    ) -> bool:
        """把调用交回事件循环。事件循环已经关掉时返回 False（程序正在退出）。"""
        try:
            loop.call_soon_threadsafe(function, *args)
        except RuntimeError:
            return False
        return True
