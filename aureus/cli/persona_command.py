"""persona 命令：查看版本历史（D62），或拿单实例锁回退三层之一（D81）。

查看不拿单实例锁，运行时也能看；回退通过 IdentityEditor 写回用户文件和版本。
数据库还不存在时直接说明，不会为了查看而新建一个空的。
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
from aureus.identity import (
    PERSONA_LAYERS,
    IdentityEdit,
    IdentityEditor,
    IdentityError,
    IdentityHistory,
    IdentityLayer,
    IdentityService,
    IdentityVersion,
    SingleInstanceLockError,
)

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
    """执行 persona 命令：列出最新版本，或按 D81 恢复指定版本。

    Args:
        args: 解析好的命令行参数，有 layer、limit、full，可指定 revert。
        paths: 数据根目录的路径集合。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 成功，1 读不了数据库。
    """
    if getattr(args, "revert", None) is not None:
        return _revert(args.revert, paths, write_line)
    if not paths.database_file.exists():
        write_line(NO_DATABASE_MESSAGE)
        return EXIT_OK
    layer = IdentityLayer(args.layer) if args.layer else None
    history = IdentityHistory(paths.database_file, SystemClock())
    try:
        history.open()
        versions = history.versions(layer, limit=args.limit or DEFAULT_PERSONA_LIMIT)
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


def _revert(version_id: int, paths: AureusPaths, write_line: WriteLine) -> int:
    """D81：先拿单实例锁，再通过编辑器恢复三层之一。"""
    history = IdentityHistory(paths.database_file, SystemClock())
    try:
        with IdentityService(paths).acquire_lock():
            if not paths.database_file.exists():
                write_line("版本不存在。")
                return EXIT_FAILURE
            try:
                history.open()
                version = history.get(version_id)
                if version is None:
                    write_line("版本不存在。")
                    return EXIT_FAILURE
                if version.layer not in PERSONA_LAYERS:
                    write_line("身份事实和不变人格请用网页设置页回退，并输入密码。")
                    return EXIT_FAILURE
                IdentityEditor(paths.identity_file, history).apply(
                    IdentityEdit(text={version.layer: version.content})
                )
                write_line(f"已把{version.layer.describe()}恢复成版本 {version_id}，下次启动生效。")
                return EXIT_OK
            finally:
                history.close()
    except SingleInstanceLockError:
        write_line("它正在运行，请用网页设置页的回退。")
    except (IdentityError, sqlite3.Error, OSError) as error:
        print(f"错误：人设回退失败（{type(error).__name__}）：{error}", file=sys.stderr)
    return EXIT_FAILURE
