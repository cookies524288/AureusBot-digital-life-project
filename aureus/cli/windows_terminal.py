"""Windows 启动器的 CLI 窗口，通过子进程的标准输入输出接到同一个心跳。"""

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
from pathlib import Path

from aureus.channels import CHANNEL_NAME, TerminalChannel
from aureus.heartbeat import Heartbeat


class WindowsTerminal:
    """管理可重复打开的终端窗口。窗口关闭后，心跳继续运行。"""

    def __init__(self, heartbeat: Heartbeat) -> None:
        self._channel = TerminalChannel(heartbeat, write_line=self._write, close_on_interrupt=False)
        heartbeat.attach_channel(CHANNEL_NAME, self._channel.show)
        self._opening = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None

    async def open(self) -> None:
        """打开一个控制台；窗口还在时重复请求复用它。"""
        async with self._opening:
            if self._process is not None and self._process.returncode is None:
                return
            if self._reader is not None:
                await self._reader
            python = Path(sys.executable).with_name("python.exe")
            process = await asyncio.create_subprocess_exec(
                str(python),
                "-X",
                "utf8",
                "-u",
                "-m",
                "aureus.cli.windows_terminal",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self._process = process
            self._reader = asyncio.create_task(self._read(process), name="aureus-windows-terminal")
            self._channel.handle_line("/help")

    def _write(self, line: str) -> None:
        process = self._process
        if process is not None and process.stdin is not None and process.returncode is None:
            process.stdin.write((line + "\n").encode("utf-8"))

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            self._channel.handle_line(line.decode("utf-8").rstrip("\r\n"))
        await process.wait()

    async def close(self) -> None:
        """结束终端进程并回收输入任务。"""
        async with self._opening:
            process = self._process
            if process is not None and process.stdin is not None:
                process.stdin.close()
            if self._reader is not None:
                await self._reader


def _forward_input() -> None:
    with (
        open("CONIN$", encoding="utf-8") as console,
        contextlib.suppress(BrokenPipeError, KeyboardInterrupt),
    ):
        for line in console:
            print(line.rstrip("\r\n"), flush=True)
    # Ctrl+Z 结束窗口端的输入，同时让主进程回收这个窗口。
    os._exit(0)


def _run_window() -> None:
    """窗口进程只转发文字。窗口关闭时，系统只结束这个子进程。"""
    threading.Thread(target=_forward_input, name="aureus-console-input", daemon=True).start()
    with (
        open("CONOUT$", "w", encoding="utf-8") as console,
        contextlib.suppress(KeyboardInterrupt),
    ):
        for line in sys.stdin:
            console.write(line)
            console.flush()


if __name__ == "__main__" and sys.platform == "win32":
    _run_window()
