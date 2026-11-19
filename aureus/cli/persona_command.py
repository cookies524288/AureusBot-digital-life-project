"""persona 命令：只读查看身份的版本历史（路线图 1.9，D62）。

和 timeline 命令一样只读、不拿单实例锁，运行时也能看。数据库还不存在时直接说明，
不会为了查看而新建一个空的。
"""

import argparse
import sqlite3
import sys

from aureus.cli.inspect_commands import (
    CONTENT_PREVIEW_CHARS,
    EXIT_FAILURE,
    EXIT_OK,
    NO_DATABASE_MESSAGE,
    WriteLine,
)
from aureus.common import AureusPaths, SystemClock
from aureus.common.time_format import format_local_time
from aureus.identity import IdentityHistory, IdentityLayer, IdentityVersion

DEFAULT_PERSONA_LIMIT = 20


def _preview(content: str) -> str:
    flat = " ".join(content.split())
    if not flat:
        return "（空）"
    if len(flat) <= CONTENT_PREVIEW_CHARS:
        return flat
    return flat[:CONTENT_PREVIEW_CHARS] + "……"


def _describe(version: IdentityVersion, *, full: bool) -> list[str]:
    head = (
        f"[{version.id}] {format_local_time(version.created_at)} "
        f"{version.layer.describe()}（{version.source.describe()}）"
    )
    if not full:
        return [f"{head}：{_preview(version.content)}"]
    body = version.content.splitlines() or ["（空）"]
    return [f"{head}：", *(f"    {line}" for line in body)]


def run_persona_command(
    args: argparse.Namespace, paths: AureusPaths, write_line: WriteLine = print
) -> int:
    """执行 persona 命令：列出身份各层最新的几个版本，从早到晚。

    Args:
        args: 解析好的命令行参数，要有 layer、limit 和 full。
        paths: 数据根目录的路径集合。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 成功，1 读不了数据库。
    """
    if not paths.database_file.exists():
        write_line(NO_DATABASE_MESSAGE)
        return EXIT_OK
    layer = IdentityLayer(args.layer) if args.layer else None
    history = IdentityHistory(paths.database_file, SystemClock())
    try:
        history.open()
        versions = history.versions(layer, limit=args.limit)
    except sqlite3.Error as error:
        print(f"错误：读不了身份的版本历史（{type(error).__name__}）：{error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        history.close()
    if not versions:
        write_line("还没有身份的版本记录：它还没有运行过。")
        return EXIT_OK
    if layer is None:
        write_line(f"各层混在一起最新的 {len(versions)} 个版本（从早到晚）：")
    else:
        write_line(
            f"{layer.describe()}最新的 {len(versions)} 个版本（从早到晚，最后一个是现在生效的）："
        )
    for version in versions:
        for line in _describe(version, full=args.full):
            write_line(line)
    return EXIT_OK
