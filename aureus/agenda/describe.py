"""日程写进上下文的样子（D74 第 5 条）：列出这个人名下未完成的约定，到期的写明要交代。"""

from collections.abc import Sequence
from datetime import datetime, tzinfo

from aureus.agenda.store import DUE_FORMAT, AgendaItem


def describe_agenda(
    items: Sequence[AgendaItem], now: datetime, timezone: tzinfo | None, *, limit: int
) -> str:
    """放进系统上下文的一段；没有未完成的条目时是空字符串。

    Args:
        items: 这个人名下未完成的条目（先到期的在前）。
        now: 此刻（UTC）。
        timezone: 显示到期时间用的时区。
        limit: 最多列几条。
    """
    if not items:
        return ""
    lines = ["【日程】你答应过这个人、还没做完的事（编号给规划时用）："]
    for item in items[:limit]:
        if item.due_at is None:
            when = "没定时间"
        else:
            local = item.due_at.astimezone(timezone).strftime(DUE_FORMAT)
            when = f"到期 {local}"
            if item.is_due(now):
                when += "，已经到期：要么做完，要么说明做不了的原因"
        lines.append(f"- #{item.id} {item.content}（{when}）")
    hidden = len(items) - limit
    if hidden > 0:
        lines.append(f"……还有 {hidden} 条没列出来。")
    return "\n".join(lines)
