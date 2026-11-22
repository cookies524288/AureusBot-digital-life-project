"""MaiBot 迁移命令的参数、JSON 输出和错误提示（路线图 3.1）。"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from aureus.cli.inspect_commands import EXIT_FAILURE, EXIT_OK
from aureus.tools.migrate.maibot import inspect_database


def configure_maibot_parser(parser: argparse.ArgumentParser) -> None:
    """注册 MaiBot 盘点命令及其源库参数。"""
    actions = parser.add_subparsers(dest="action", required=True, metavar="操作")
    inspect = actions.add_parser("inspect", help="以 JSON 输出 SQLite 结构与记录数（只读）")
    inspect.add_argument("source", type=Path, metavar="SQLite 文件")


def run_maibot_inspect_command(source: Path) -> int:
    """盘点指定源库，成功时输出 JSON，失败时输出诊断并返回 1。"""
    try:
        report = inspect_database(source)
    except FileNotFoundError:
        print("错误：源文件不存在。", file=sys.stderr)
        return EXIT_FAILURE
    except OSError:
        print("错误：无法访问源文件。", file=sys.stderr)
        return EXIT_FAILURE
    except sqlite3.Error as error:
        # SQLite 的原始错误文本可能包含源库的 SQL 或私有路径。
        primary_code = error.sqlite_errorcode & 0xFF
        if primary_code in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT):
            message = "源文件已损坏或不是 SQLite 数据库"
        else:
            message = "无法只读盘点 SQLite 数据库"
        print(f"错误：{message}（{error.sqlite_errorname}）。", file=sys.stderr)
        return EXIT_FAILURE
    print(json.dumps(report, indent=2))
    return EXIT_OK
