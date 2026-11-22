"""MaiBot 迁移命令的参数、JSON 输出和脱敏诊断。"""

import argparse
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

from aureus.cli.inspect_commands import EXIT_FAILURE, EXIT_OK
from aureus.tools.migrate.maibot import inspect_database
from aureus.tools.migrate.maibot_catalog import BUSINESS_TABLES, MigrationError
from aureus.tools.migrate.maibot_package import Source, export_package, preview_package


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", required=True, help="稳定的逻辑实例名，同一来源换路径时沿用")
    parser.add_argument(
        "--role", required=True, metavar="角色", help="源库角色：" + "、".join(BUSINESS_TABLES)
    )


def configure_maibot_parser(parser: argparse.ArgumentParser) -> None:
    """注册 MaiBot 盘点、导出和预演命令。"""
    parser.epilog = "顺序：inspect 盘点 → export 导出私有包 → preview 预演。"
    actions = parser.add_subparsers(dest="action", required=True, metavar="操作")
    inspect = actions.add_parser("inspect", help="以 JSON 输出 SQLite 结构与记录数（只读）")
    inspect.add_argument("source", type=Path, metavar="SQLite 文件")
    export = actions.add_parser("export", help="备份单个 SQLite 来源并导出业务行")
    _add_source_arguments(export)
    export.add_argument("source", type=Path, metavar="SQLite 源文件")
    export.add_argument("output", type=Path, metavar="新 ZIP 包")
    preview = actions.add_parser("preview", help="校验私有包并预演逐表对账")
    _add_source_arguments(preview)
    preview.add_argument("package", type=Path, metavar="ZIP 包")


def run_maibot_command(args: argparse.Namespace) -> int:
    """执行迁移操作并输出脱敏结果。"""
    if args.action == "inspect":
        return run_maibot_inspect_command(args.source)
    try:
        source = Source(args.instance, args.role)
        if args.action == "export":
            result = export_package(source, args.source, args.output)
        else:
            result = preview_package(source, args.package)
        report = result.report(args.action)
    except MigrationError as error:
        details = {
            name: value
            for name, value in {"table": error.table, "row": error.row}.items()
            if value is not None
        }
        print(json.dumps({"error": error.code, **details}), file=sys.stderr)
        return EXIT_FAILURE
    except (zipfile.BadZipFile, EOFError, NotImplementedError, RuntimeError):
        print(json.dumps({"error": "invalid_package"}), file=sys.stderr)
        return EXIT_FAILURE
    except (OSError, sqlite3.Error, UnicodeError) as error:
        code = "sqlite_failure" if isinstance(error, sqlite3.Error) else "file_failure"
        print(json.dumps({"error": code}), file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}), file=sys.stderr)
        return 130
    print(json.dumps(report, indent=2))
    return EXIT_OK


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
