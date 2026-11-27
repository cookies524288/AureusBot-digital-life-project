"""已授权的群清单筛选及本地通用梗词典；不使用群内学到的黑话。"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class GroupMembership:
    """平台确认的群号与群名。"""

    group_id: str
    name: str


@dataclass(frozen=True)
class GroupMembershipResult:
    """主人可见的匹配结果，截断名单时仍能报告匹配总数。"""

    matches: tuple[GroupMembership, ...]
    total_matches: int


def find_group_memberships(
    groups: Sequence[GroupMembership], query: str, *, limit: int
) -> GroupMembershipResult:
    """筛选平台响应中的群，不按同名合并不同群。"""
    seen: set[str] = set()
    matches: list[GroupMembership] = []
    needle = query.strip().casefold()
    for group in groups:
        if group.group_id in seen:
            continue
        seen.add(group.group_id)
        if needle in group.group_id.casefold() or needle in group.name.casefold():
            matches.append(group)
    return GroupMembershipResult(tuple(matches[:limit]), len(matches))


@dataclass(frozen=True)
class MemeEntry:
    """可公开的词名、用法与来源。"""

    title: str
    usage: str
    source: str


def lookup_meme(entries: Sequence[MemeEntry], query: str) -> MemeEntry | None:
    """命中多个名称时只返回最具体的释义。"""
    text = query.strip().casefold()
    return max(
        (item for item in entries if item.title and item.title.casefold() in text),
        key=lambda item: len(item.title),
        default=None,
    )


def format_meme_result(entry: MemeEntry | None) -> str:
    """给规划器实际的本地释义，未命中时明确告知未知。"""
    if not entry:
        return "本地通用词典没查到这个梗，不能确定它的意思。"
    return f"【梗】{entry.title}\n【用法】{entry.usage}\n【来源】{entry.source}"
