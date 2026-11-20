"""查看兴趣与最近收获、结束兴趣；不拿单实例锁（D79）。"""

import argparse
import sqlite3
import sys
from contextlib import ExitStack

from aureus.agenda import FindingStore, InterestStore
from aureus.cli.inspect_commands import EXIT_FAILURE, EXIT_OK, NO_DATABASE_MESSAGE, WriteLine
from aureus.common import AureusPaths, ConfigError, SystemClock, load_runtime_config
from aureus.common.time_format import format_local_time

_RECENT_FINDINGS = 5


def run_interests_command(
    args: argparse.Namespace, paths: AureusPaths, write_line: WriteLine = print
) -> int:
    """列在用兴趣与收获，或按用户要求结束一条；返回零表示成功。"""
    if not paths.database_file.exists():
        write_line(NO_DATABASE_MESSAGE)
        return EXIT_FAILURE if args.remove is not None else EXIT_OK
    try:
        config = load_runtime_config(paths).interests
        clock = SystemClock()
        interests = InterestStore(paths.database_file, clock, config)
        findings = FindingStore(paths.database_file, clock)
        with ExitStack() as stack:
            for store in (interests, findings):
                store.open()
                stack.callback(store.close)
            if args.remove is not None:
                item = interests.remove(args.remove, "user_removed")
                if item is None:
                    print(f"错误：没有第 {args.remove} 条在用的兴趣", file=sys.stderr)
                    return EXIT_FAILURE
                write_line(f"已结束：#{item.id} {item.text}")
                return EXIT_OK
            write_line("在用兴趣：")
            for item in interests.active():
                origin = "起始" if item.origin == "seed" else "自己加的"
                write_line(
                    f"#{item.id} [{origin}] {item.text}（{format_local_time(item.created_at)}）"
                )
            write_line("最近收获：")
            for finding in findings.recent(_RECENT_FINDINGS):
                shared = "已分享" if finding.shared_at is not None else "未分享"
                write_line(
                    f"{format_local_time(finding.created_at)} [{shared}] {finding.text}"
                    f"（来自网页《{finding.page_title}》）"
                )
    except (ConfigError, sqlite3.Error) as error:
        print(f"错误：读写不了兴趣（{type(error).__name__}）：{error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK
