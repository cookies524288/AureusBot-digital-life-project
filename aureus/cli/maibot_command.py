"""MaiBot 迁移命令的参数、JSON 输出和脱敏诊断。"""

import argparse
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aureus.cli.inspect_commands import EXIT_FAILURE, EXIT_OK
from aureus.common import AureusPaths
from aureus.memory import MemoryIndex
from aureus.timeline import TimelineDatabase
from aureus.tools.migrate.maibot import inspect_database, open_readonly_database
from aureus.tools.migrate.maibot_catalog import BUSINESS_TABLES, MigrationError
from aureus.tools.migrate.maibot_memory import publish_maibot_memory
from aureus.tools.migrate.maibot_package import Source, export_package, preview_package
from aureus.tools.migrate.maibot_staging import import_package, restore_snapshot


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", required=True, help="稳定的逻辑实例名，同一来源换路径时沿用")
    parser.add_argument(
        "--role", required=True, metavar="角色", help="源库角色：" + "、".join(BUSINESS_TABLES)
    )


def configure_maibot_parser(parser: argparse.ArgumentParser) -> None:
    """注册 MaiBot 迁移命令及其参数。"""
    parser.epilog = (
        "顺序：inspect 盘点 → export 导出私有包 → preview 预演 → "
        "import 写入独立暂存库。restore 可从暂存库恢复源快照。"
    )
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
    stage = actions.add_parser("import", help="将已校验的包导入独立 SQLite 暂存库")
    _add_source_arguments(stage)
    stage.add_argument("package", type=Path, metavar="ZIP 包")
    stage.add_argument("target", type=Path, metavar="SQLite 暂存库")
    restore = actions.add_parser("restore", help="从暂存库验证并恢复完整源快照")
    _add_source_arguments(restore)
    restore.add_argument("target", type=Path, metavar="SQLite 暂存库")
    restore.add_argument("output", type=Path, metavar="新 SQLite 文件")
    memory = actions.add_parser("publish-memory", help="将已暂存的主库和记忆库发布到指定 Aureus")
    memory.add_argument("staging", type=Path, metavar="SQLite 暂存库")
    memory.add_argument("--instance", required=True, help="暂存时使用的逻辑实例名")
    memory.add_argument("--source-timezone", required=True, help="源库本地时间的 IANA 时区")
    memory.add_argument("--main-media-root", type=Path, help="MaiBot 主库附件根目录")
    memory.add_argument("--memory-media-root", type=Path, help="A_Memorix 图片资产根目录")
    memory.add_argument(
        "--target-home", required=True, type=Path, help="独立 Aureus 目标数据根目录"
    )


def run_maibot_command(args: argparse.Namespace) -> int:
    """执行迁移操作并输出脱敏结果。"""
    if args.action == "inspect":
        return run_maibot_inspect_command(args.source)
    try:
        if args.action == "publish-memory":
            report = _publish_memory(args)
        elif args.action == "export":
            source = Source(args.instance, args.role)
            report = export_package(source, args.source, args.output).report("export")
        elif args.action == "preview":
            report = preview_package(Source(args.instance, args.role), args.package).report(
                "preview"
            )
        elif args.action == "import":
            report = import_package(Source(args.instance, args.role), args.package, args.target)
        else:
            report = restore_snapshot(Source(args.instance, args.role), args.target, args.output)
    except MigrationError as error:
        details = {
            name: value
            for name, value in {"table": error.table, "row": error.row}.items()
            if value is not None
        }
        print(json.dumps({"error": error.code, **details}), file=sys.stderr)
        return EXIT_FAILURE
    except (zipfile.BadZipFile, EOFError):
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


def _publish_memory(args: argparse.Namespace) -> dict[str, object]:
    try:
        zone = ZoneInfo(args.source_timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise MigrationError("invalid_source_timezone") from error
    paths = AureusPaths(args.target_home.resolve())
    stage = args.staging.resolve(strict=True)
    target = paths.database_file
    if target.exists() and target.samefile(stage):
        raise MigrationError("staging_is_memory_target")
    if target.exists():
        with closing(open_readonly_database(target)) as database:
            if (
                database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('timeline', 'memory_history')"
                ).fetchone()
                is None
            ):
                raise MigrationError("not_aureus_memory_target")
    roots = {"main": args.main_media_root, "memory": args.memory_media_root}
    for root in roots.values():
        if root is not None and root.resolve().is_relative_to(paths.home):
            raise MigrationError("media_root_is_in_memory_target")
    for location in (paths.data_dir, target, paths.memory_dir):
        if not location.resolve().is_relative_to(paths.home):
            raise MigrationError("memory_target_outside_home")
    index = MemoryIndex(target, TimelineDatabase(target))
    return publish_maibot_memory(stage, args.instance, zone, index, roots, paths.memory_dir)


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
