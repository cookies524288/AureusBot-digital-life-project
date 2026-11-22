"""Windows 图形启动入口，网页和终端共用同一个 Aureus 实例。"""

import argparse
import asyncio
import ctypes
import os
import sys
from collections.abc import Callable
from threading import Thread
from time import sleep

from aureus.common import AureusPaths, ConfigError, SystemClock
from aureus.common.paths import HomePathError, resolve_home
from aureus.common.vault import VaultError
from aureus.core.runtime import Core, open_core
from aureus.identity import IdentityError, SingleInstanceLockError
from aureus.web.assemble import open_web
from aureus.web.runner import LOOPBACK_HOST, WebServer, WebServerError, running_web_url

from .windows_terminal import WindowsTerminal

RESTART_EXIT_CODE = 75
_STARTUP_POLLS = 80
_STARTUP_POLL_INTERVAL = 0.1


def _open_running_web(paths: AureusPaths) -> bool:
    url = running_web_url(paths.home)
    if url is None:
        return False
    os.startfile(url)
    return True


async def _run_web(
    core: Core,
    web: WebServer,
    terminal: WindowsTerminal,
    request_restart: Callable[[], None],
    *,
    tray: bool,
    opening_browser: bool,
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
        if opening_browser:
            os.startfile(url)

    async def serve() -> None:
        try:
            await web.run(on_started=open_browser)
        finally:
            core.heartbeat.request_stop()

    async def beat() -> None:
        try:
            await core.heartbeat.run()
        finally:
            web.request_stop()

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(serve())
            tasks.create_task(beat())
    finally:
        await terminal.close()


def _launch(paths: AureusPaths, *, fake: bool, tray: bool, opening_browser: bool) -> bool:
    if _open_running_web(paths):
        return False
    restarting = False
    clock = SystemClock()
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
                    raise ConfigError("请在 aureus.toml 的 [web] 中启用网页，或运行 aureus chat。")
                try:
                    asyncio.run(
                        _run_web(
                            core,
                            web,
                            terminal,
                            request_restart,
                            tray=tray,
                            opening_browser=opening_browser,
                        )
                    )
                except* (WebServerError, OSError) as errors:
                    raise ConfigError(str(errors.exceptions[0])) from None
    except SingleInstanceLockError:
        # 同时双击时，先拿到核心锁的进程可能还没开始监听。
        for _ in range(_STARTUP_POLLS):
            if _open_running_web(paths):
                return False
            sleep(_STARTUP_POLL_INTERVAL)
        raise
    return restarting


def main() -> int:
    """从 Windows 启动器运行，启动错误显示在系统对话框中。"""
    if sys.platform != "win32":
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--tray", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        paths = AureusPaths(resolve_home())
        opening_browser = not args.restarted
        while _launch(paths, fake=args.fake, tray=args.tray, opening_browser=opening_browser):
            if args.tray:
                return RESTART_EXIT_CODE
            opening_browser = False
    except (ConfigError, HomePathError, VaultError, IdentityError, OSError) as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), "Aureus 启动失败", 0x10)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
