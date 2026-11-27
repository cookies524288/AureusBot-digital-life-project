"""chat 命令：启动核心，在终端里和它聊天（路线图 0.10）。

用法：`uv run python -m aureus --home 数据根目录 chat`；加 `--fake` 用假器官，不调用模型、不花钱。
"""

import argparse
import asyncio
import sys
from collections.abc import Callable
from contextlib import nullcontext

from aureus.channels import CHANNEL_NAME, TerminalChannel
from aureus.channels.onebot import account_id
from aureus.common import AureusPaths, ConfigError, SystemClock, WebConfig
from aureus.common.async_cleanup import finish_after
from aureus.common.vault import VaultError
from aureus.core.runtime import Core, open_core
from aureus.identity.service import IdentityError
from aureus.web.assemble import open_web
from aureus.web.runner import LOOPBACK_HOST, WebServer, WebServerError

EXIT_OK = 0
EXIT_FAILURE = 1

ReadLine = Callable[[], str]
WriteLine = Callable[[str], None]


def qq_group_argument(value: str) -> str:
    """按 OneBot 的账号格式校验本次允许收发的群号。"""
    group = account_id(value)
    if group is None:
        raise argparse.ArgumentTypeError("请输入有效的 QQ 群号")
    return group


async def _serve_web(web: WebServer, config: WebConfig, write_line: WriteLine) -> None:
    """跑网页服务；没能启动（比如端口被占用）只提示一行，不影响终端和心跳（D63）。"""

    def show_address() -> None:
        where = "本机地址，已开放局域网访问" if config.allow_remote else "本机可以打开"
        write_line(f"网页：http://{LOOPBACK_HOST}:{config.port}（{where}）")

    try:
        await web.run(on_started=show_address)
    except WebServerError as error:
        write_line(f"网页没有开启：{error}")


async def _chat(
    core: Core,
    web: WebServer | None,
    terminal: TerminalChannel,
    write_line: WriteLine,
    *,
    first: bool,
) -> None:
    """先让心跳跳起来，再接上终端和网页；心跳停下（/quit、输入结束或出错）时返回。"""
    heartbeat = core.heartbeat
    if not first:
        terminal.reconnect(heartbeat)
    heartbeat.attach_channel(CHANNEL_NAME, terminal.show)
    serving: asyncio.Task[None] | None = None

    async def stop_web() -> None:
        if web is not None and serving is not None:
            await web.stop(serving)

    async def serve(server: WebServer) -> None:
        try:
            await _serve_web(server, core.settings.runtime.web, write_line)
        except BaseException:
            heartbeat.request_stop()
            raise

    async with finish_after(heartbeat.finalize_shutdown), core.qq or nullcontext():
        beating = asyncio.create_task(heartbeat.run(finalize=False), name="aureus-heartbeat")
        async with finish_after(stop_web):
            try:
                await asyncio.sleep(0)
                if heartbeat.downtime is not None:
                    write_line(f"{heartbeat.bot_pronoun}醒来了。{heartbeat.downtime.describe()}")
                if web is not None:
                    serving = asyncio.create_task(serve(web), name="aureus-web")
                if first:
                    terminal.start()
            except BaseException:
                heartbeat.request_stop()
                async with finish_after(lambda: beating):
                    raise
            await beating


def run_chat_command(
    args: argparse.Namespace,
    paths: AureusPaths,
    read_line: ReadLine = input,
    write_line: WriteLine = print,
) -> int:
    """执行 chat 命令。

    Args:
        args: 解析好的命令行参数，要有 fake。
        paths: 数据根目录的路径集合。
        read_line: 读一行输入的函数；测试时传假的。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 正常关闭，1 启动失败。
    """
    if args.fake:
        write_line("用的是假器官：不调用模型，不花钱。")

    async def run() -> None:
        terminal: TerminalChannel | None = None
        while True:
            restarting = False
            with open_core(
                paths, fake_organs=args.fake, qq_test_group=getattr(args, "qq_test_group", None)
            ) as core:

                def request_restart() -> None:
                    nonlocal restarting
                    restarting = True
                    core.heartbeat.request_stop()

                first = terminal is None
                if terminal is None:
                    terminal = TerminalChannel(
                        core.heartbeat, read_line, write_line, request_restart=request_restart
                    )
                with open_web(core, SystemClock(), request_restart=request_restart) as web:
                    await _chat(core, web, terminal, write_line, first=first)
            if not restarting:
                return

    try:
        asyncio.run(run())
    except (ConfigError, VaultError, IdentityError) as error:
        # 启动前就发现的问题：说清楚原因就退出，不带一大段追踪信息。
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        # 心跳被取消时已经走完关闭流程（写了 shut_down），这里只是安静地退出。
        write_line("已关闭。")
        return EXIT_OK
    write_line("已关闭。")
    return EXIT_OK
