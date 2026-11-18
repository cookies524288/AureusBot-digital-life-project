"""timeline 和 usage 命令：只读查看时间线和模型用量（路线图 0.10）。

两个命令都只读，不拿单实例锁，所以它在运行时也能查看。数据库文件还不存在时直接说明，
不会为了查看而新建一个空的。
"""

import argparse
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from aureus.common import AureusPaths, ConfigError, SystemClock, load_runtime_config
from aureus.common.time_format import format_local_time
from aureus.heartbeat.events import EVENT_ORGAN_CALL_ABANDONED
from aureus.organs.interface import sum_costs
from aureus.safety import Ledger, LedgerEntry, month_bounds
from aureus.timeline import TimelineDatabase, TimelineQuery, TimelineRecord

EXIT_OK = 0
EXIT_FAILURE = 1

DEFAULT_TIMELINE_LIMIT = 20
# 时间线每条内容最多显示多少个字，超过的用省略号结尾。
CONTENT_PREVIEW_CHARS = 60
# 读账本时每次取多少条。
LEDGER_PAGE_SIZE = 500

WriteLine = Callable[[str], None]
NO_DATABASE_MESSAGE = "还没有数据库：它还没有运行过。"


def positive_int(text: str) -> int:
    """命令行参数类型：必须是正整数。"""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要一个正整数，但收到了 {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"需要一个正整数，但收到了 {text!r}")
    return value


def _preview(content: str) -> str:
    flat = " ".join(content.split())
    if len(flat) <= CONTENT_PREVIEW_CHARS:
        return flat
    return flat[:CONTENT_PREVIEW_CHARS] + "……"


def _describe_record(record: TimelineRecord) -> str:
    actor = record.actor_type.value
    if record.actor_id:
        actor += f":{record.actor_id}"
    return (
        f"[{record.id}] {format_local_time(record.timestamp)} {actor} "
        f"{record.event_type}（{record.visibility.value}）：{_preview(record.content)}"
    )


def run_timeline_command(
    args: argparse.Namespace, paths: AureusPaths, write_line: WriteLine = print
) -> int:
    """执行 timeline 命令：列出时间线上最新的几条记录。

    Args:
        args: 解析好的命令行参数，要有 limit 和 event_type。
        paths: 数据根目录的路径集合。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 成功，1 读不了数据库。
    """
    if not paths.database_file.exists():
        write_line(NO_DATABASE_MESSAGE)
        return EXIT_OK
    timeline = TimelineDatabase(paths.database_file)
    try:
        timeline.open()
        records = timeline.query(
            TimelineQuery(limit=args.limit, latest=True, event_type=args.event_type or "")
        )
        total = timeline.count_records()
    except sqlite3.Error as error:
        print(f"错误：读不了时间线（{type(error).__name__}）：{error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        timeline.close()
    write_line(f"时间线共 {total} 条记录，下面是符合条件的最新 {len(records)} 条：")
    for record in records:
        write_line(_describe_record(record))
    return EXIT_OK


@dataclass
class _OrganUsage:
    calls: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    costs: list[float | None] = field(default_factory=list)


def _entries_between(ledger: Ledger, start: datetime, end: datetime) -> list[LedgerEntry]:
    """分页读出账本里 [start, end) 之间的记录。"""
    selected: list[LedgerEntry] = []
    since_id = 0
    while True:
        page = ledger.entries(since_id=since_id, limit=LEDGER_PAGE_SIZE)
        if not page:
            return selected
        selected.extend(entry for entry in page if start <= entry.timestamp < end)
        since_id = page[-1].id


def _count_abandoned_calls(timeline: TimelineDatabase, start: datetime, end: datetime) -> int:
    """数时间线里 [start, end) 之间被放弃的器官调用：这些调用用掉的 token 不在账本里。"""
    count = 0
    since_id = 0
    while True:
        page = timeline.query(
            TimelineQuery(
                since_id=since_id, limit=LEDGER_PAGE_SIZE, event_type=EVENT_ORGAN_CALL_ABANDONED
            )
        )
        if not page:
            return count
        count += sum(1 for record in page if start <= record.timestamp < end)
        since_id = page[-1].id


def _format_cost(costs: list[float | None]) -> str:
    total = sum_costs(costs)
    # None 是“算不出来”，不是 0：没写价格时不能显示成免费（D37）。
    return "未知" if total is None else f"{total:.4f} 元"


def run_usage_command(paths: AureusPaths, write_line: WriteLine = print) -> int:
    """执行 usage 命令：本月（UTC 自然月）的模型用量，总数和按器官分开的数。

    Args:
        paths: 数据根目录的路径集合。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 成功，1 配置或账本读不了。
    """
    try:
        limit = load_runtime_config(paths).safety.monthly_token_limit
    except ConfigError as error:
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_FAILURE
    if not paths.database_file.exists():
        write_line(NO_DATABASE_MESSAGE)
        return EXIT_OK
    start, end = month_bounds(SystemClock().now())
    ledger = Ledger(paths.database_file)
    timeline = TimelineDatabase(paths.database_file)
    try:
        ledger.open()
        entries = _entries_between(ledger, start, end)
        timeline.open()
        abandoned = _count_abandoned_calls(timeline, start, end)
    except sqlite3.Error as error:
        print(f"错误：读不了账本（{type(error).__name__}）：{error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        ledger.close()
        timeline.close()

    by_organ: dict[str, _OrganUsage] = {}
    for entry in entries:
        usage = by_organ.setdefault(entry.organ_id, _OrganUsage())
        usage.calls += 1
        usage.failed += 0 if entry.succeeded else 1
        usage.prompt_tokens += entry.prompt_tokens
        usage.completion_tokens += entry.completion_tokens
        usage.costs.append(entry.cost)
    prompt = sum(usage.prompt_tokens for usage in by_organ.values())
    completion = sum(usage.completion_tokens for usage in by_organ.values())
    total = prompt + completion
    failed = sum(usage.failed for usage in by_organ.values())
    estimated = sum(1 for entry in entries if entry.estimated)

    write_line(f"本月（UTC {start:%Y-%m}）模型用量：{len(entries)} 次请求，失败 {failed} 次，")
    write_line(f"其中 {estimated} 次的用量是估算的。")
    if abandoned:
        # 关闭时取消的调用来不及记账（0.8 已知缺口，审查 H3），要让用户看得见。
        write_line(
            f"另有 {abandoned} 次调用在关闭时被取消，它们的用量和费用未知、没有计入下面的数字，"
            "实际用量可能比显示的多。"
        )
    write_line(f"token：输入 {prompt} + 输出 {completion} = {total}")
    if limit is None:
        write_line("月度上限：没有设置（在 aureus.toml 的 [safety] 里写 monthly_token_limit）")
    else:
        write_line(f"月度上限：{limit}，已用 {total / limit:.1%}")
    all_costs = [cost for usage in by_organ.values() for cost in usage.costs]
    write_line(f"费用：{_format_cost(all_costs)}（只用来显示；价格没写时是未知，不代表免费）")
    for organ_id in sorted(by_organ):
        usage = by_organ[organ_id]
        write_line(
            f"  {organ_id}：{usage.calls} 次（失败 {usage.failed}），"
            f"输入 {usage.prompt_tokens}，输出 {usage.completion_tokens}，"
            f"费用 {_format_cost(usage.costs)}"
        )
    return EXIT_OK
