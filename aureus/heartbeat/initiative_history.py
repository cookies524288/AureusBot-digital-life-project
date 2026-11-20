"""从只追加的时间线恢复主动联系的历史，不恢复发送通道（D76）。"""

from dataclasses import dataclass
from datetime import datetime

from aureus.common import BudgetCalendar
from aureus.heartbeat.events import (
    EVENT_INITIATIVE_CONSIDERED,
    EVENT_INITIATIVE_RESULT,
    EVENT_MESSAGE,
)
from aureus.heartbeat.initiative import InitiativeState
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery

# 设计规定只向后看最近 50 次，避免醒来时扫描整条时间线。
_RESTORE_LIMIT = 50


@dataclass(frozen=True)
class InitiativeHistory:
    """恢复出的计数和待结算记录；状态仍由心跳维护。"""

    state: InitiativeState
    considered_id: int | None = None
    feedback_id: int | None = None


def read_initiative_history(
    timeline: TimelineDatabase, calendar: BudgetCalendar, now: datetime
) -> InitiativeHistory:
    """读最近的考虑和结果；用户后来开过口，就不再沿用此前的退避和等待。"""
    considered = timeline.query(
        TimelineQuery(event_type=EVENT_INITIATIVE_CONSIDERED, latest=True, limit=_RESTORE_LIMIT)
    )
    results = timeline.query(
        TimelineQuery(event_type=EVENT_INITIATIVE_RESULT, latest=True, limit=_RESTORE_LIMIT)
    )
    users = timeline.query(
        TimelineQuery(event_type=EVENT_MESSAGE, actor_type=ActorType.USER, latest=True, limit=1)
    )
    latest_user_id = users[-1].id if users else 0
    streak = 0
    for result in reversed(results):
        source_id = result.metadata.get("considered_id")
        reset_id = source_id if isinstance(source_id, int) else result.id
        if reset_id < latest_user_id or result.metadata.get("answered") is not False:
            break
        streak += 1
    sent = [record for record in considered if record.metadata.get("sent") is True]
    settled = {record.metadata.get("considered_id") for record in results}
    latest = sent[-1] if sent else None
    pending = (
        latest
        if latest is not None and latest.id not in settled and latest.id > latest_user_id
        else None
    )
    state = InitiativeState(
        contacts_today=sum(
            calendar.day_of(record.timestamp) == calendar.day_of(now) for record in sent
        ),
        last_considered_at=considered[-1].timestamp if considered else None,
        unanswered_streak=streak,
        pending_since=pending.timestamp if pending is not None else None,
    )
    feedback_id = pending.metadata.get("feedback_id") if pending is not None else None
    if not isinstance(feedback_id, int) or isinstance(feedback_id, bool):
        feedback_id = None
    return InitiativeHistory(state, pending.id if pending is not None else None, feedback_id)
