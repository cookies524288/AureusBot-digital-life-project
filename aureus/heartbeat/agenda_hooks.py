"""从主循环搬来的日程接线；显式接收日程、反馈和时间线（D80）。"""

import logging
import sqlite3
from datetime import tzinfo
from typing import Any

from aureus.agenda import AgendaItem, AgendaStore, ItemStatus, describe_agenda, parse_due
from aureus.common import (
    Clock,
)
from aureus.feedback import COMMITMENT_KEPT, PRIOR_CONFIDENCE, FeedbackError, FeedbackStore, Outcome
from aureus.heartbeat.events import (
    EVENT_AGENDA_ADDED,
    EVENT_AGENDA_CLOSED,
    EVENT_AGENDA_DUE,
)
from aureus.heartbeat.planning import (
    Plan,
    ResolutionResult,
)
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineRecord,
    VisibilityScope,
)

logger = logging.getLogger(__name__)

_STATUS_WORDS = {
    ItemStatus.DONE: "做完了",
    ItemStatus.DROPPED: "做不了",
    ItemStatus.EXPIRED: "过期了",
    ItemStatus.CANCELLED: "被用户取消了",
}


def _item_metadata(item: AgendaItem) -> dict[str, Any]:
    """日程事件写进时间线的 metadata。"""
    return {
        "item_id": item.id,
        "person_id": item.person_id,
        "content": item.content,
        "due_at": item.due_at.isoformat() if item.due_at is not None else None,
        "status": item.status.value,
        "reason": item.reason,
    }


class AgendaHooks:
    """在拍里处理约定和反馈；不拥有主循环。"""

    def __init__(
        self,
        agenda: AgendaStore | None,
        timeline: TimelineDatabase,
        clock: Clock,
        feedback: FeedbackStore | None,
        timezone: tzinfo | None,
    ) -> None:
        self._agenda, self._timeline, self._clock = agenda, timeline, clock
        self._feedback, self._timezone = feedback, timezone

    def _apply_agenda(self, plan: Plan, person_id: int, source: TimelineRecord) -> None:
        """按规划器的建议记下答应的事、结束做完或做不了的约定（D74 第 3、6 条）。

        器官只提建议，代码校验后写入；写不进去只记日志，不影响已经说出口的回复。
        """
        agenda = self._agenda
        if agenda is None:
            return
        try:
            for resolution in plan.resolutions:
                item = agenda.get(resolution.item_id)
                if item is None or item.person_id != person_id:
                    continue  # 只认这个人名下的条目
                status = (
                    ItemStatus.DONE
                    if resolution.result is ResolutionResult.DONE
                    else ItemStatus.DROPPED
                )
                self._finish_item(item, status, resolution.reason)
            if plan.commit is not None:
                self._add_item(person_id, plan.commit.what, plan.commit.due_text, source)
        except sqlite3.Error:
            logger.exception("日程写不进去，这次的约定没有记下")

    def _add_item(
        self, person_id: int, what: str, due_text: str | None, source: TimelineRecord
    ) -> None:
        agenda = self._agenda
        assert agenda is not None
        now = self._clock.now()
        due = parse_due(due_text, now, self._timezone, agenda.config.max_due_days)
        feedback_id = self._expect_commitment(source.id)
        item = agenda.add(
            person_id, what, due_at=due, source_record_id=source.id, feedback_id=feedback_id
        )
        if item is None:
            logger.warning("这个人名下未完成的约定已满，新的没有记下")
            return
        self._timeline.append(
            event_type=EVENT_AGENDA_ADDED,
            content=f"记下了答应的事 #{item.id}：{item.content}",
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata=_item_metadata(item),
        )

    def _expect_commitment(self, source_id: int) -> int | None:
        """记一条承诺的反馈预期，把握取这一类当前的自信（没有数据时用先验，D74 第 6 条）。"""
        feedback = self._feedback
        if feedback is None:
            return None
        try:
            confidence = feedback.confidence(COMMITMENT_KEPT).value
            expected = PRIOR_CONFIDENCE if confidence is None else confidence
            return feedback.record_expectation(COMMITMENT_KEPT, expected, source_id)
        except (FeedbackError, sqlite3.Error):
            logger.exception("承诺的反馈预期没记上")
            return None

    def _finish_item(self, item: AgendaItem, status: ItemStatus, reason: str) -> None:
        """结束一条约定，记时间线；有反馈预期的补写结果（取消的不算成败）。"""
        agenda = self._agenda
        assert agenda is not None
        finished = agenda.finish(item.id, status, reason)
        if finished is None:
            return
        self._timeline.append(
            event_type=EVENT_AGENDA_CLOSED,
            content=f"约定 #{item.id}「{item.content}」{_STATUS_WORDS[status]}"
            + (f"：{finished.reason}" if finished.reason else ""),
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata=_item_metadata(finished),
        )
        if self._feedback is None or item.feedback_id is None or status is ItemStatus.CANCELLED:
            return
        outcome = Outcome.SUCCESS if status is ItemStatus.DONE else Outcome.FAILURE
        try:
            self._feedback.record_outcome(item.feedback_id, outcome)
        except (FeedbackError, sqlite3.Error):
            logger.exception("承诺 #%s 的反馈结果没补上", item.id)

    def _check_agenda(self) -> None:
        """每拍：到期的约定记一次提醒，过了宽限还没交代的算过期（D74 第 5、6 条）。

        日程读写失败只记日志，不让心跳停下：约定晚一拍处理不要紧。
        """
        agenda = self._agenda
        if agenda is None:
            return
        now = self._clock.now()
        try:
            for item in agenda.overdue(now):
                self._finish_item(item, ItemStatus.EXPIRED, "到期后一直没交代")
            for item in agenda.due_unnoticed(now):
                agenda.mark_due_noticed(item.id)
                self._timeline.append(
                    event_type=EVENT_AGENDA_DUE,
                    content=f"答应的事 #{item.id} 到期了：{item.content}",
                    actor_type=ActorType.ME,
                    visibility=VisibilityScope.PRIVATE,
                    metadata=_item_metadata(item),
                )
        except sqlite3.Error:
            logger.exception("日程读不了，这一拍不检查到期")

    def _agenda_for_context(self, person_id: int | None) -> str:
        """这个人名下未完成的约定；没有日程或认不出是谁时不放。"""
        agenda = self._agenda
        if agenda is None or person_id is None:
            return ""
        try:
            items = agenda.open_items(person_id)
        except sqlite3.Error:
            logger.exception("日程读不了，这次上下文里不放日程")
            return ""
        return describe_agenda(
            items, self._clock.now(), self._timezone, limit=agenda.config.context_items
        )
