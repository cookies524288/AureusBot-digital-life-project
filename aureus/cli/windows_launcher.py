"""Windows 图形启动入口，网页和终端共用同一个 Aureus 实例。"""

import argparse
import asyncio
import base64
import ctypes
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import httpx

from aureus.common import AureusPaths, ConfigError, SystemClock, home_is_default, resolve_home
from aureus.common.async_cleanup import finish_after
from aureus.common.paths import PROJECT_ROOT, HomePathError
from aureus.common.vault import VaultError
from aureus.core.runtime import Core, open_core
from aureus.identity import IdentityError, SingleInstanceLockError
from aureus.web.assemble import open_web
from aureus.web.runner import LOOPBACK_HOST, WebServer, WebServerError, running_web_instance

from .windows_terminal import WindowsTerminal

RESTART_EXIT_CODE = 75
# 双击时另一个实例可能正在启动或重启：旧进程释放单实例锁、新进程发布网页地址都要时间。
# 正常关闭可能超过 100 秒，等待要覆盖它，期间不另起内核（3.49、D144）。
_STARTUP_WAIT_SECONDS = 180.0
_STARTUP_POLL_INTERVAL = 0.1
_HOME_POINTER = "aureus-home.txt"
_LOCK_PID_FILE = "aureus.lock.pid"
_LAUNCHER_COMMAND = re.compile(r"(?<!\S)-m\s+aureus\.cli\.windows_launcher(?=\s|$)")
_PROCESS_QUERY_TIMEOUT_SECONDS = 5
_TASKKILL_TIMEOUT_SECONDS = 10
_TASKKILL_SETTLE_SECONDS = 1.0
# MessageBox 的按钮和图标：是/否 + 问号；返回 6 表示点了“是”。
_MB_YESNO = 0x4
_MB_ICONQUESTION = 0x20
_IDYES = 6


def _launcher_home() -> Path:
    if not home_is_default():
        return resolve_home()
    pointer = PROJECT_ROOT / _HOME_POINTER
    try:
        home = Path(pointer.read_text(encoding="utf-8-sig").strip())
    except FileNotFoundError:
        return resolve_home()
    except UnicodeDecodeError as error:
        raise HomePathError(f"请将 {pointer} 保存为 UTF-8 文本。") from error
    if not home.is_absolute() or not home.is_dir():
        raise HomePathError(f"请在 {pointer} 中填写现有数据根目录的绝对路径。")
    return home


def _open_running_web(paths: AureusPaths, *, tray: bool) -> bool:
    instance = running_web_instance(paths.home)
    if instance is None:
        return False
    url, owner = instance
    # 共享内存里的地址可能属于已经挂住的旧内核：根地址 1 秒内没有 200 就不附着（D144）。
    try:
        healthy = httpx.get(url, trust_env=False, timeout=1).status_code == 200
    except (httpx.HTTPError, httpx.InvalidURL):
        healthy = False
    if not healthy:
        return False
    if tray:
        print(f"AUREUS_WEB_ATTACH={owner} {url}", flush=True)
    else:
        os.startfile(url)
    return True


def _confirm_takeover(pid: int) -> bool:
    """让维护者确认是否结束挂住的旧启动器（D144）。"""
    message = (
        f"另一个 Aureus 实例（进程 {pid}）正在运行，但网页已经无响应。\n"
        "它可能是上次重启后残留的进程。要结束这个进程树并继续启动吗？\n"
        "未保存的网页操作会丢失。"
    )
    answer = ctypes.windll.user32.MessageBoxW(
        None, message, "Aureus 启动", _MB_YESNO | _MB_ICONQUESTION
    )
    return answer == _IDYES


def _launcher_process(pid: int) -> tuple[str, datetime] | None:
    """读取 PID 对应的命令行和创建时刻；进程不存在或查询失败时返回 None。"""
    query = (
        "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = "
        + str(pid)
        + "' -ErrorAction Stop; "
        + "ConvertTo-Json -InputObject @($p | Select-Object CommandLine, "
        + "@{Name='Started'; Expression={$_.CreationDate.ToUniversalTime().ToString('o')}}) "
        + "-Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True,
            text=True,
            timeout=_PROCESS_QUERY_TIMEOUT_SECONDS,
            check=True,
        )
        records = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list) or len(records) != 1:
        return None
    record = records[0]
    if not isinstance(record, dict):
        return None
    command_line = record.get("CommandLine")
    started = record.get("Started")
    if not isinstance(command_line, str) or not isinstance(started, str):
        return None
    try:
        created = datetime.fromisoformat(started)
    except ValueError:
        return None
    return (command_line, created) if created.tzinfo is not None else None


def _takeover_stale_instance(paths: AureusPaths) -> bool:
    """核对并结束挂住的旧启动器；没有核对通过或维护者取消时不碰任何进程（D144）。"""
    pid_file = paths.home / _LOCK_PID_FILE
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        locked_at = datetime.fromtimestamp(pid_file.stat().st_mtime, UTC)
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    process = _launcher_process(pid)
    if process is None:
        return False
    command_line, created = process
    if not _LAUNCHER_COMMAND.search(command_line) or created >= locked_at:
        return False
    if not _confirm_takeover(pid):
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT_SECONDS,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    # taskkill 只表示结束请求已被接受；稍等再重试，让锁随进程退出释放（D144）。
    sleep(_TASKKILL_SETTLE_SECONDS)
    return True


async def _run_web(
    core: Core,
    web: WebServer,
    terminal: WindowsTerminal,
    request_restart: Callable[[], None],
    *,
    tray: bool,
    opening_browser: bool,
    owner_pid: int | None,
) -> None:
    port = core.settings.runtime.web.port
    url = f"http://{LOOPBACK_HOST}:{port}"

    if tray:
        loop = asyncio.get_running_loop()

        def wait_for_tray_exit() -> None:
            # 启动器发 R 请求重启，关闭管道请求退出。
            command = os.read(sys.stdin.fileno(), 1)
            if not loop.is_closed():
                loop.call_soon_threadsafe(
                    request_restart if command == b"R" else core.heartbeat.request_stop
                )

        Thread(target=wait_for_tray_exit, daemon=True).start()

    def open_browser() -> None:
        if tray:
            print(f"AUREUS_WEB_URL={url}", flush=True)
        elif opening_browser:
            os.startfile(url)

    async def serve() -> None:
        try:
            await web.run(on_started=open_browser, owner_pid=owner_pid)
        finally:
            core.heartbeat.request_stop()

    async with (
        finish_after(core.heartbeat.finalize_shutdown),
        finish_after(terminal.close),
        core.qq or nullcontext(),
    ):
        serving = asyncio.create_task(serve(), name="aureus-web")
        async with finish_after(lambda: web.stop(serving)):
            await core.heartbeat.run(finalize=False)


def _launch(
    paths: AureusPaths, *, fake: bool, tray: bool, opening_browser: bool, owner_pid: int | None
) -> bool:
    restarting = False
    clock = SystemClock()
    deadline = monotonic() + _STARTUP_WAIT_SECONDS
    while True:
        if _open_running_web(paths, tray=tray):
            return False
        try:
            with open_core(paths, fake_organs=fake, clock=clock, to_console=False) as core:

                def request_restart() -> None:
                    nonlocal restarting
                    restarting = True
                    core.heartbeat.request_stop()

                terminal = WindowsTerminal(core.heartbeat, request_restart)
                with open_web(
                    core, clock, open_terminal=terminal.open, request_restart=request_restart
                ) as web:
                    if web is None:
                        raise ConfigError(
                            "请在 aureus.toml 的 [web] 中启用网页，或运行 aureus chat。"
                        )
                    try:
                        asyncio.run(
                            _run_web(
                                core,
                                web,
                                terminal,
                                request_restart,
                                tray=tray,
                                opening_browser=opening_browser,
                                owner_pid=owner_pid,
                            )
                        )
                    except WebServerError as error:
                        raise ConfigError(str(error)) from error
            return restarting
        except SingleInstanceLockError:
            # 另一个实例正在运行：可能还没发布网页地址，也可能正在重启。
            # 等它发布后附着；锁释放了就由下一轮正常启动，不另开第二个内核。
            if monotonic() < deadline:
                sleep(_STARTUP_POLL_INTERVAL)
                continue
            if not _takeover_stale_instance(paths):
                raise
            # 旧进程可能还要一点时间才退出并释放锁；重新给一轮等待，
            # 避免同一次启动里反复弹出确认框（D144）。
            deadline = monotonic() + _STARTUP_WAIT_SECONDS


def main() -> int:
    """从 Windows 启动器运行，启动错误显示在系统对话框中。"""
    if sys.platform != "win32":
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--tray", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--owner-pid", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        paths = AureusPaths(_launcher_home())
        opening_browser = not args.restarted
        while _launch(
            paths,
            fake=args.fake,
            tray=args.tray,
            opening_browser=opening_browser,
            owner_pid=args.owner_pid,
        ):
            if args.tray:
                return RESTART_EXIT_CODE
            opening_browser = False
    except (ConfigError, HomePathError, VaultError, IdentityError, OSError) as error:
        if args.tray:
            reason = base64.b64encode(str(error).encode()).decode("ascii")
            print(f"AUREUS_WEB_ERROR={reason}", flush=True)
        else:
            ctypes.windll.user32.MessageBoxW(None, str(error), "Aureus 启动失败", 0x10)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
