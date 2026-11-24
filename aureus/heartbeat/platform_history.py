"""为会话规划读取已获准的旧平台原文，不返回附件回址或写入任何记忆。"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from aureus.timeline import VisibilityScope
from aureus.timeline.platform_history import LegacyPlatformHistory

from .inbox_store import ConversationKey, InboxStore
from .recall import MAX_RECALL_CHARS

MAX_HISTORY_ROWS = 20
_LEGACY_SCAN_SIZE = 100


@dataclass(frozen=True)
class HistoryBoundary:
    """两个来源使用不同的序号；边界针对其中一个来源且不包含该条。"""

    source: Literal["inbox", "timeline"]
    id: int


@dataclass(frozen=True)
class HistoryQuery:
    """规划器只能决定查询条件，不能决定其所属会话。"""

    limit: int
    keyword: str | None
    before: HistoryBoundary | None


@dataclass(frozen=True)
class _HistoryRow:
    source: Literal["inbox", "timeline"]
    id: int
    timestamp: datetime
    sender_id: str | None
    sender_name: str | None
    direction: str
    platform_message_id: str | None
    content: str

    def detail(self) -> dict[str, object]:
        return {**asdict(self), "timestamp": self.timestamp.isoformat()}


def _encode(rows: list[dict[str, object]], before: HistoryBoundary | None) -> str:
    return json.dumps(
        {
            "status": "ok" if rows else "no_results",
            "messages": rows,
            "next_before": asdict(before) if before else None,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _result(rows: list[_HistoryRow], limit: int, more: HistoryBoundary | None) -> str:
    messages: list[dict[str, object]] = []
    for index, row in enumerate(rows[:limit]):
        before = HistoryBoundary(row.source, row.id) if index + 1 < len(rows) or more else None
        detail = row.detail()
        detail["content"] = (
            row.content[:MAX_RECALL_CHARS] + "…"
            if len(row.content) > MAX_RECALL_CHARS
            else row.content
        )
        result = _encode([*messages, detail], before)
        if len(result) > MAX_RECALL_CHARS:
            if messages:
                previous = rows[index - 1]
                return _encode(messages, HistoryBoundary(previous.source, previous.id))
            detail["content"] = ""
            if len(_encode([detail], before)) > MAX_RECALL_CHARS:
                return _encode([], HistoryBoundary(row.source, row.id))
            lower, upper = 0, min(len(row.content), MAX_RECALL_CHARS)
            while lower < upper:
                middle = (lower + upper + 1) // 2
                detail["content"] = row.content[:middle] + "…"
                if len(_encode([detail], before)) <= MAX_RECALL_CHARS:
                    lower = middle
                else:
                    upper = middle - 1
            detail["content"] = row.content[:lower] + "…" if lower else ""
            return _encode([detail], before)
        messages.append(detail)
    if messages and len(rows) > limit:
        return _encode(messages, HistoryBoundary(rows[limit - 1].source, rows[limit - 1].id))
    return _encode(messages, more)


class PlatformHistory:
    """组合当前账号的原件及有精确归属的旧时间线。默认没有已核准路由。"""

    def __init__(self, store: InboxStore, legacy: LegacyPlatformHistory) -> None:
        self.store, self.legacy = store, legacy
        self.allows: Callable[[ConversationKey], bool] | None = None

    def permits(self, key: ConversationKey) -> bool:
        """每次都复验当前通道的账号及会话范围。"""
        return key.platform == "qq" and self.allows is not None and self.allows(key)

    def read(self, key: ConversationKey, visibility: VisibilityScope, query: HistoryQuery) -> str:
        """读取一页并生成有界工具结果；视图新旧来源之间不漏去重。"""
        rows: list[_HistoryRow] = []
        limit = min(query.limit, MAX_HISTORY_ROWS)
        before = query.before
        if before is None or before.source == "inbox":
            for message in self.store.list_read_history(
                key,
                visibility,
                before_id=before.id if before else None,
                keyword=query.keyword,
                limit=limit + 1,
            ):
                perception = message.perception
                rows.append(
                    _HistoryRow(
                        "inbox",
                        message.id,
                        message.timestamp,
                        perception.sender_id or None,
                        message.sender_name or None,
                        message.direction,
                        perception.route.message_id if perception.route else None,
                        perception.content,
                    )
                )
        if len(rows) > limit:
            return _result(rows, limit, None)
        legacy_before = before.id if before and before.source == "timeline" else None
        old = self.legacy.page(
            (key.platform, key.account_id, key.kind, key.recipient_id, key.temporary_group_id),
            before_id=legacy_before,
            limit=_LEGACY_SCAN_SIZE,
            visibility=visibility,
            keyword=query.keyword,
            received_only=True,
        )
        identifiers = [item.metadata.get("message_id") for item in old]
        canonical = self.store.existing_message_ids(
            key, (value for value in identifiers if isinstance(value, str) and value)
        )
        excluded = self.store.excluded_timeline_ids(key, (record.id for record in old))
        for record, identifier in zip(old, identifiers, strict=True):
            platform_id = identifier if isinstance(identifier, str) and identifier else None
            if platform_id in canonical or record.id in excluded:
                continue
            name = record.metadata.get("sender_name")
            rows.append(
                _HistoryRow(
                    "timeline",
                    record.id,
                    record.timestamp,
                    record.actor_id,
                    name if isinstance(name, str) else None,
                    "incoming",
                    platform_id,
                    record.content,
                )
            )
        more = HistoryBoundary("timeline", old[-1].id) if len(old) == _LEGACY_SCAN_SIZE else None
        return _result(rows, limit, more)
