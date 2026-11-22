"""Windows 图形启动入口，网页和终端共用同一个 Aureus 实例。"""

import argparse
import asyncio
import ctypes
import os
import sys

from aureus.common import AureusPaths, ConfigError, SystemClock
from aureus.common.paths import HomePathError, resolve_home
from aureus.common.vault import VaultError
from aureus.core.runtime import Core, open_core
from aureus.identity.service import IdentityError
from aureus.web.assemble import open_web
from aureus.web.runner import LOOPBACK_HOST, WebServer, WebServerError

from .windows_terminal import WindowsTerminal


async def _run_web(core: Core, web: WebServer, terminal: WindowsTerminal) -> None:
    port = core.settings.runtime.web.port

    def open_browser() -> None:
        os.startfile(f"http://{LOOPBACK_HOST}:{port}")

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


def main() -> int:
    """从 Windows 启动器运行，启动错误显示在系统对话框中。"""
    if sys.platform != "win32":
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true")
    args = parser.parse_args()
    try:
        paths = AureusPaths(resolve_home())
        clock = SystemClock()
        with open_core(paths, fake_organs=args.fake, clock=clock, to_console=False) as core:
            terminal = WindowsTerminal(core.heartbeat)
            with open_web(core, clock, open_terminal=terminal.open) as web:
                if web is None:
                    raise ConfigError("请在 aureus.toml 的 [web] 中启用网页，或运行 aureus chat。")
                try:
                    asyncio.run(_run_web(core, web, terminal))
                except* (WebServerError, OSError) as errors:
                    raise ConfigError(str(errors.exceptions[0])) from None
    except (ConfigError, HomePathError, VaultError, IdentityError, OSError) as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), "Aureus 启动失败", 0x10)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
