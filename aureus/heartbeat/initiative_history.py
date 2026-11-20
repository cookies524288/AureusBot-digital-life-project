"""从主动消息和回应结果还原次数、退避和等待，不合并残缺记录（D76、D78）。"""

from dataclasses import dataclass
from datetime import datetime

from aureus.common import BudgetCalendar, InitiativeConfig
from aureus.heartbeat import events
from aureus.heartbeat.initiative import InitiativeState
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, TimelineRecord

_RESTORE_LIMIT = 50


@dataclass(frozen=True)
class InitiativeHistory:
    """恢复出的把关状态和最近一次尚未结算的主动消息。"""

    state: InitiativeState
    message: TimelineRecord | None = None
    answer_at: datetime | None = None


def read_initiative_history(
    timeline: TimelineDatabase, calendar: BudgetCalendar, now: datetime, config: InitiativeConfig
) -> InitiativeHistory:
    """消息就是联系凭据；考虑记录只用于恢复上次考虑的时刻（D78）。"""
    messages = timeline.query(
        TimelineQuery(
            event_type=events.EVENT_MESSAGE,
            actor_type=ActorType.ME,
            metadata={"initiative": True},
            latest=True,
            limit=_RESTORE_LIMIT,
        )
    )
    results = timeline.query(
        TimelineQuery(event_type=events.EVENT_INITIATIVE_RESULT, latest=True, limit=_RESTORE_LIMIT)
    )
    considered = timeline.query(
        TimelineQuery(event_type=events.EVENT_INITIATIVE_CONSIDERED, latest=True, limit=1)
    )
    users = timeline.query(
        TimelineQuery(
            event_type=events.EVENT_MESSAGE, actor_type=ActorType.USER, latest=True, limit=1
        )
    )
    latest_user_id = users[-1].id if users else 0
    streak = 0
    for result in reversed(results):
        if result.metadata["message_id"] < latest_user_id or result.metadata["answered"]:
            break
        streak += 1
    settled = {result.metadata["message_id"] for result in results}
    pending = messages[-1] if messages and messages[-1].id not in settled else None
    answers = (
        timeline.query(
            TimelineQuery(
                event_type=events.EVENT_MESSAGE,
                actor_type=ActorType.USER,
                since_id=pending.id,
                limit=1,
            )
        )
        if pending is not None
        else []
    )
    day = calendar.day_of(now)
    count = sum(calendar.day_of(message.timestamp) == day for message in messages)
    if len(messages) == _RESTORE_LIMIT and calendar.day_of(messages[0].timestamp) == day:
        count = max(count, config.max_per_day)
    return InitiativeHistory(
        InitiativeState(
            contacts_today=count,
            last_considered_at=considered[-1].timestamp if considered else None,
            unanswered_streak=streak,
            pending_since=pending.timestamp if pending is not None else None,
        ),
        pending,
        answers[0].timestamp if answers else None,
    )
