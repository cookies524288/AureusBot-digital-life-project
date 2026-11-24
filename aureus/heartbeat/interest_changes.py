"""用户私聊中的兴趣建议和上下文；身份由心跳确认（D79）。"""

import logging
from datetime import datetime, tzinfo

from aureus.agenda import FindingStore, InterestStore
from aureus.common import BudgetCalendar
from aureus.heartbeat import events
from aureus.heartbeat.context import describe_interests
from aureus.heartbeat.planning import InterestAction, InterestChange
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

logger = logging.getLogger(__name__)
_CONTEXT_FINDINGS = 2


def interests_enabled(interests: InterestStore | None) -> bool:
    """兴趣建议是否已装配并开启，不读取任何实际兴趣。"""
    return interests is not None and interests.config.enabled


def interest_context(
    interests: InterestStore | None,
    findings: FindingStore | None,
    *,
    is_user: bool,
    visibility: VisibilityScope,
    timezone: tzinfo | None,
) -> str:
    """兴趣关着、群聊或非用户对话时完全不注入兴趣信息。"""
    if not is_user or visibility is not VisibilityScope.USER_ONLY:
        return ""
    if not interests_enabled(interests):
        return ""
    assert interests is not None
    try:
        recent = findings.recent(_CONTEXT_FINDINGS) if findings is not None else []
        return describe_interests(interests.active(), recent, timezone)
    except Exception:
        logger.exception("兴趣上下文读不了")
        return ""


def apply_interest(
    store: InterestStore | None,
    timeline: TimelineDatabase,
    calendar: BudgetCalendar,
    change: InterestChange,
    source: TimelineRecord,
    now: datetime,
) -> None:
    """只在已确认用户的私聊应用建议；表或留痕失败只记日志，不补记。"""
    if store is None or not store.config.enabled:
        return
    limit = store.config.max_changes_per_day
    if source.visibility is not VisibilityScope.USER_ONLY or not limit:
        return
    try:
        query = TimelineQuery(
            event_type=events.EVENT_INTEREST_CHANGED,
            actor_type=ActorType.ME,
            latest=True,
            limit=limit,
        )
        records = timeline.query(query)
        changes = sum(calendar.day_of(r.timestamp) == calendar.day_of(now) for r in records)
        if changes >= limit:
            return
        if change.action is InterestAction.ADD:
            item = store.add(change.text or "", "self", source.id)
        elif change.action is InterestAction.REMOVE:
            item = store.remove(change.interest_id or 0, "self_removed")
        else:
            item = store.replace(change.interest_id or 0, change.text or "", source.id)
        if item is not None:
            timeline.append(
                events.EVENT_INTEREST_CHANGED,
                f"兴趣改动：{item.text}",
                ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata={
                    "action": change.action.value,
                    "interest_id": change.interest_id or item.id,
                    "new_interest_id": None if change.action is InterestAction.REMOVE else item.id,
                    "text": item.text,
                    "reason": change.reason,
                    "source_record_id": source.id,
                },
            )
    except Exception:
        logger.exception("兴趣改动或留痕失败，不补记")
