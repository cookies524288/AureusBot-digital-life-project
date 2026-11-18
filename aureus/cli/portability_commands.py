"""export 和 import 命令：把数据导出成 zip，或从 zip 导入（D45，路线图 0.10）。

导出不含密钥，运行中也能导出（只读备份）；导入要求没有实例在运行，目标已有数据时默认拒绝。
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from aureus.common import AureusPaths, SystemClock
from aureus.core.portability import PortabilityError, export_data, import_data

EXIT_OK = 0
EXIT_FAILURE = 1

WriteLine = Callable[[str], None]


def archive_argument(text: str) -> Path:
    """命令行参数类型：导出包的文件名。相对路径按此刻所在的目录算。"""
    if not text.strip():
        raise argparse.ArgumentTypeError("文件名不能是空的")
    return Path(text.strip()).expanduser().resolve()


def run_export_command(
    args: argparse.Namespace, paths: AureusPaths, write_line: WriteLine = print
) -> int:
    """执行 export 命令。

    Args:
        args: 解析好的命令行参数，要有 archive 和 include_logs。
        paths: 数据根目录的路径集合。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 成功，1 失败。
    """
    try:
        result = export_data(paths, args.archive, SystemClock(), include_logs=args.include_logs)
    except PortabilityError as error:
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_FAILURE
    write_line(f"已导出 {result.file_count} 个文件（{result.total_bytes} 字节）到 {result.archive}")
    write_line("导出包里没有密钥；到新位置导入后，用 keys set 或 keys migrate 重新录入。")
    return EXIT_OK


def run_import_command(
    args: argparse.Namespace, paths: AureusPaths, write_line: WriteLine = print
) -> int:
    """执行 import 命令。

    Args:
        args: 解析好的命令行参数，要有 archive、force 和 no_backup。
        paths: 数据根目录的路径集合。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 成功，1 失败（什么都没有改）。
    """
    if args.no_backup and not args.force:
        print("错误：--no-backup 要和 --force 一起用。", file=sys.stderr)
        return EXIT_FAILURE
    try:
        result = import_data(
            paths, args.archive, SystemClock(), force=args.force, backup_first=not args.no_backup
        )
    except PortabilityError as error:
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_FAILURE
    write_line(f"已导入 {result.file_count} 个文件。")
    if result.backup is not None:
        write_line(f"覆盖前的旧数据已备份到 {result.backup}")
    elif args.force and args.no_backup:
        write_line("按你的要求没有备份，被覆盖的旧数据找不回来了。")
    write_line("导入之后，旧位置的实例应该停用：同一份数据不能同时由两个实例运行。")
    return EXIT_OK
