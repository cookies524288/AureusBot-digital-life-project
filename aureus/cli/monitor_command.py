"""monitor 命令：启动观察台（Streamlit，只读，只绑本机，D64）。

观察台是独立进程，只读数据库、不拿单实例锁，所以和 aureus chat 能同时开。
没装 Streamlit 时给中文提示，不报堆栈。
"""

import argparse
import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import aureus.monitor
from aureus.cli.inspect_commands import EXIT_FAILURE, EXIT_OK, NO_DATABASE_MESSAGE, WriteLine
from aureus.common import AureusPaths

# 只绑本机：观察台能看到时间线里的私聊，不对局域网开放（D64 第 8 条）。
LOCAL_ADDRESS = "127.0.0.1"
# 网页通道用 8767，观察台用 8766。
DEFAULT_MONITOR_PORT = 8766
MIN_PORT = 1024
MAX_PORT = 65535
PAGE_FILE = Path(aureus.monitor.__file__).parent / "page.py"
NOT_INSTALLED_MESSAGE = (
    "观察台需要 Streamlit，它是可选依赖，还没有装。装法：uv sync --extra monitor"
)

Launcher = Callable[[list[str]], int]


def streamlit_installed() -> bool:
    """当前环境里有没有 Streamlit。"""
    return importlib.util.find_spec("streamlit") is not None


def build_command(paths: AureusPaths, port: int) -> list[str]:
    """启动 Streamlit 的命令行：只绑本机、不自动开浏览器、关掉使用统计。"""
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PAGE_FILE),
        "--server.address",
        LOCAL_ADDRESS,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--",
        "--home",
        str(paths.home),
    ]


def _launch(command: list[str]) -> int:
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return EXIT_OK


def run_monitor_command(
    args: argparse.Namespace,
    paths: AureusPaths,
    launch: Launcher = _launch,
    write_line: WriteLine = print,
) -> int:
    """执行 monitor 命令：启动观察台，Ctrl+C 退出。

    Args:
        args: 解析好的命令行参数，要有 port。
        paths: 数据根目录的路径集合。
        launch: 运行命令行并返回退出码的函数；测试时传假的。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 正常，1 端口不合法或没装 Streamlit。
    """
    if not MIN_PORT <= args.port <= MAX_PORT:
        print(f"错误：端口要在 {MIN_PORT} 到 {MAX_PORT} 之间。", file=sys.stderr)
        return EXIT_FAILURE
    if not streamlit_installed():
        print(NOT_INSTALLED_MESSAGE, file=sys.stderr)
        return EXIT_FAILURE
    if not paths.database_file.exists():
        write_line(NO_DATABASE_MESSAGE)
        return EXIT_OK
    write_line(f"观察台地址：http://{LOCAL_ADDRESS}:{args.port}（只有本机能打开，Ctrl+C 退出）")
    return launch(build_command(paths, args.port))
