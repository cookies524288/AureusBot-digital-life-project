"""agenda 命令：查看它答应过的事，取消某一条（路线图 2.7，D74 第 7 条）。

不拿单实例锁，运行中也能用：查看只读；取消只把条目状态改成「用户取消」，
并在时间线记一条（算用户做的事），不算承诺成败。数据库还不存在时直接说明，不新建。
"""

import argparse
import sqlite3
import sys

from aureus.agenda import AgendaItem, AgendaStore, ItemStatus
from aureus.cli.inspect_commands import (
    EXIT_FAILURE,
    EXIT_OK,
    NO_DATABASE_MESSAGE,
    WriteLine,
)
from aureus.common import AureusPaths, ConfigError, SystemClock, load_runtime_config
from aureus.common.time_format import format_local_time
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope

DEFAULT_AGENDA_LIMIT = 30
# 和心跳里的 EVENT_AGENDA_CLOSED 同名；cli 不依赖心跳，这里按值写。
_EVENT_AGENDA_CLOSED = "agenda_closed"
_STATUS_NAMES = {
    ItemStatus.OPEN: "未完成",
    ItemStatus.DONE: "做完了",
    ItemStatus.DROPPED: "做不了",
    ItemStatus.EXPIRED: "过期了",
    ItemStatus.CANCELLED: "已取消",
}


def _describe(item: AgendaItem) -> str:
    due = format_local_time(item.due_at) if item.due_at is not None else "没定时间"
    tail = f"：{item.reason}" if item.reason else ""
    return (
        f"#{item.id} [{_STATUS_NAMES[item.status]}] {item.content}"
        f"（对象 {item.person_id}，到期 {due}）{tail}"
    )


def run_agenda_command(
    args: argparse.Namespace, paths: AureusPaths, write_line: WriteLine = print
) -> int:
    """执行 agenda 命令：不带子命令时列出条目；cancel 编号 取消一条。

    Returns:
        退出码：0 成功，1 读写失败或编号不对。
    """
    if not paths.database_file.exists():
        write_line(NO_DATABASE_MESSAGE)
        return EXIT_OK
    try:
        config = load_runtime_config(paths).agenda
    except ConfigError as error:
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_FAILURE
    clock = SystemClock()
    store = AgendaStore(paths.database_file, clock, config)
    try:
        store.open()
        if args.cancel is not None:
            return _cancel(store, paths, args.cancel, write_line)
        items = store.recent(args.limit) if args.all else store.open_items()
    except sqlite3.Error as error:
        print(f"错误：读写不了日程（{type(error).__name__}）：{error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        store.close()
    if not items:
        write_line("没有未完成的约定。" if not args.all else "还没有任何约定。")
        return EXIT_OK
    for item in items:
        write_line(_describe(item))
    return EXIT_OK


def _cancel(store: AgendaStore, paths: AureusPaths, item_id: int, write_line: WriteLine) -> int:
    finished = store.finish(item_id, ItemStatus.CANCELLED, "用户在命令行取消")
    if finished is None:
        print(f"错误：没有第 {item_id} 条未完成的约定", file=sys.stderr)
        return EXIT_FAILURE
    timeline = TimelineDatabase(paths.database_file, SystemClock())
    try:
        timeline.open()
        timeline.append(
            event_type=_EVENT_AGENDA_CLOSED,
            content=f"约定 #{finished.id}「{finished.content}」被用户取消了",
            actor_type=ActorType.USER,
            visibility=VisibilityScope.PRIVATE,
            metadata={"item_id": finished.id, "status": finished.status.value},
        )
    finally:
        timeline.close()
    write_line(f"已取消：{_describe(finished)}")
    return EXIT_OK
