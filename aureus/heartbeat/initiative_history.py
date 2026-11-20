"""从只追加的时间线恢复主动联系的历史，不恢复发送通道（D76）。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aureus.common import BudgetCalendar, InitiativeConfig
from aureus.feedback import PROACTIVE_CONTACT, FeedbackStore
from aureus.heartbeat.delivery import read_delivery_receipt, read_message_delivery
from aureus.heartbeat.events import (
    EVENT_INITIATIVE_CONSIDERED,
    EVENT_INITIATIVE_RESULT,
    EVENT_MESSAGE,
)
from aureus.heartbeat.initiative import InitiativeState
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, TimelineRecord

# 设计规定只向后看最近 50 次相关记录，普通聊天不能挤掉尚未结算的主动联系。
_RESTORE_LIMIT = 50


@dataclass(frozen=True)
class InitiativeHistory:
    """恢复出的计数和待结算材料；用户已回应不等于结果已持久化。"""

    state: InitiativeState
    considered_id: int | None = None
    feedback_id: int | None = None
    message: TimelineRecord | None = None
    answer_at: datetime | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class _Contact:
    """同一次联系的消息与考虑记录；考虑记录丢失时以消息保守占用限额。"""

    record: TimelineRecord
    message: TimelineRecord | None
    considered_id: int | None

    @property
    def source(self) -> TimelineRecord:
        return self.message if self.message is not None else self.record


def _read_contacts(
    timeline: TimelineDatabase,
    considered: Sequence[TimelineRecord],
    messages: Sequence[TimelineRecord],
) -> list[_Contact]:
    messages_by_id = {message.id: message for message in messages}
    linked: set[int] = set()
    contacts: list[_Contact] = []
    previous_id = 0
    for record in considered:
        message_id = record.metadata.get("message_id")
        message = messages_by_id.get(message_id) if isinstance(message_id, int) else None
        if "message_id" not in record.metadata:
            # 兼容升级前的记录：一次考虑至多写一条主动消息，且消息先于考虑结果。
            candidates = [item for item in messages if previous_id < item.id < record.id]
            message = candidates[-1] if candidates else None
        previous_id = record.id
        if message is not None:
            linked.add(message.id)
        if record.metadata.get("sent") is True or (
            record.metadata.get("sent") is None and isinstance(message_id, int)
        ):
            contacts.append(_Contact(record, message, record.id))
    for message in messages:
        if message.id in linked:
            continue
        # 消息和考虑窗口可能错位：先按来源精确找回，不能把已知发送失败当成未知。
        matches = timeline.query(
            TimelineQuery(
                event_type=EVENT_INITIATIVE_CONSIDERED, metadata={"message_id": message.id}, limit=1
            )
        )
        if matches:
            record = matches[0]
            if record.metadata.get("sent") is not False:
                contacts.append(_Contact(record, message, record.id))
        elif read_message_delivery(timeline, message) is not False:
            contacts.append(_Contact(message, message, None))
    return sorted(contacts, key=lambda contact: contact.source.id)


def _contact_time(timeline: TimelineDatabase, contact: _Contact) -> datetime:
    if contact.message is not None:
        receipt = read_delivery_receipt(timeline, contact.message)
        if receipt is not None and receipt.metadata.get("delivered") is True:
            return receipt.timestamp
    return contact.source.timestamp


def read_initiative_history(
    timeline: TimelineDatabase,
    calendar: BudgetCalendar,
    now: datetime,
    config: InitiativeConfig,
    feedback: FeedbackStore | None = None,
) -> InitiativeHistory:
    """合并消息和考虑记录，按来源去重；未落结果的联系即使已收到来信也继续恢复。"""
    considered = timeline.query(
        TimelineQuery(event_type=EVENT_INITIATIVE_CONSIDERED, latest=True, limit=_RESTORE_LIMIT)
    )
    results = timeline.query(
        TimelineQuery(event_type=EVENT_INITIATIVE_RESULT, latest=True, limit=_RESTORE_LIMIT)
    )
    messages = timeline.query(
        TimelineQuery(
            event_type=EVENT_MESSAGE,
            actor_type=ActorType.ME,
            metadata={"initiative": True},
            latest=True,
            limit=_RESTORE_LIMIT,
        )
    )
    users = timeline.query(
        TimelineQuery(event_type=EVENT_MESSAGE, actor_type=ActorType.USER, latest=True, limit=1)
    )
    latest_user_id = users[-1].id if users else 0
    contacts = _read_contacts(timeline, considered, messages)
    by_considered = {contact.considered_id: contact for contact in contacts}
    streak = 0
    for result in reversed(results):
        source_id = result.metadata.get("considered_id")
        contact = by_considered.get(source_id)
        reset_id = contact.source.id if contact is not None else source_id
        if not isinstance(reset_id, int):
            reset_id = result.id
        if reset_id < latest_user_id or result.metadata.get("answered") is not False:
            break
        streak += 1
    settled = {record.metadata.get("considered_id") for record in results}
    settled_messages = {record.metadata.get("message_id") for record in results}
    pending = next(
        (
            contact
            for contact in reversed(contacts)
            if (contact.considered_id is None or contact.considered_id not in settled)
            and (contact.message is None or contact.message.id not in settled_messages)
        ),
        None,
    )
    day = calendar.day_of(now)
    sent_at = {contact.source.id: _contact_time(timeline, contact) for contact in contacts}
    count = sum(calendar.day_of(moment) == day for moment in sent_at.values())
    if any(
        len(records) == _RESTORE_LIMIT and calendar.day_of(records[0].timestamp) == day
        for records in (considered, messages)
    ):
        # 同一天已超出恢复窗口时无法证明还剩次数，宁可当天停下，也不低估后继续刷屏。
        count = max(count, config.max_per_day)
    state = InitiativeState(
        contacts_today=count,
        last_considered_at=max(
            [record.timestamp for record in considered] + list(sent_at.values()),
            default=None,
        ),
        unanswered_streak=streak,
        pending_since=sent_at[pending.source.id] if pending is not None else None,
    )
    if pending is None:
        return InitiativeHistory(state)
    answers = timeline.query(
        TimelineQuery(
            event_type=EVENT_MESSAGE, actor_type=ActorType.USER, since_id=pending.source.id, limit=1
        )
    )
    feedback_id = pending.record.metadata.get("feedback_id")
    if not isinstance(feedback_id, int) or isinstance(feedback_id, bool):
        feedback_id = None
    confirmed = pending.record.metadata.get("sent") is True
    if pending.message is not None:
        confirmed = confirmed or read_message_delivery(timeline, pending.message) is True
        if feedback is not None and feedback_id is None:
            expectation = feedback.find_by_source(PROACTIVE_CONTACT, pending.message.id)
            if expectation is not None:
                feedback_id = expectation.id
                confirmed = True
    return InitiativeHistory(
        state,
        pending.considered_id,
        feedback_id,
        pending.message,
        answers[0].timestamp if answers else None,
        confirmed,
    )
