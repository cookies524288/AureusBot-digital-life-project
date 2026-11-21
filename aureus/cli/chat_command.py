"""chat 命令：启动核心，在终端里和它聊天（路线图 0.10）。

用法：`uv run python -m aureus --home 数据根目录 chat`；加 `--fake` 用假器官，不调用模型、不花钱。
"""

import argparse
import asyncio
import sys
from collections.abc import Callable

from aureus.channels import CHANNEL_NAME, TerminalChannel
from aureus.common import AureusPaths, ConfigError, SystemClock, WebConfig
from aureus.common.vault import VaultError
from aureus.core.runtime import Core, open_core
from aureus.identity.service import IdentityError
from aureus.web.assemble import open_web
from aureus.web.runner import LOOPBACK_HOST, WebServer, WebServerError

EXIT_OK = 0
EXIT_FAILURE = 1

ReadLine = Callable[[], str]
WriteLine = Callable[[str], None]


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
    core: Core, web: WebServer | None, read_line: ReadLine, write_line: WriteLine
) -> None:
    """先让心跳跳起来，再接上终端和网页；心跳停下（/quit、输入结束或出错）时返回。"""
    heartbeat = core.heartbeat
    terminal = TerminalChannel(heartbeat, read_line=read_line, write_line=write_line)
    heartbeat.attach_channel(CHANNEL_NAME, terminal.show)
    # 心跳先醒来再接终端：醒来时要先读时间线算停机多久，用户敲的第一句话才不会抢在前面。
    beating = asyncio.create_task(heartbeat.run(), name="aureus-heartbeat")
    await asyncio.sleep(0)
    if heartbeat.downtime is not None:
        write_line(f"它醒来了。{heartbeat.downtime.describe()}")
    serving: asyncio.Task[None] | None = None
    if web is not None:
        serving = asyncio.create_task(
            _serve_web(web, core.settings.runtime.web, write_line), name="aureus-web"
        )
    terminal.start()
    try:
        await beating
    finally:
        # 心跳停了（或被取消）网页也跟着停：网页消息已经没人回复了。
        if web is not None and serving is not None:
            web.request_stop()
            await serving


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
    try:
        with (
            open_core(paths, fake_organs=args.fake) as core,
            open_web(core, SystemClock()) as web,
        ):
            asyncio.run(_chat(core, web, read_line, write_line))
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
