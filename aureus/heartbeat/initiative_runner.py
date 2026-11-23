"""主动联系的计数、等待和记录；写入失败就少主动，不保存补记材料（D76、D78）。"""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from aureus.common import BudgetCalendar, InitiativeConfig
from aureus.feedback import PRIOR_CONFIDENCE, PROACTIVE_CONTACT, FeedbackStore, Outcome
from aureus.heartbeat.events import (
    EVENT_INITIATIVE_CONSIDERED,
    EVENT_INITIATIVE_RESULT,
    EVENT_MESSAGE,
)
from aureus.heartbeat.initiative import InitiativeDecision, InitiativeState
from aureus.heartbeat.initiative_history import read_initiative_history
from aureus.heartbeat.perception import Perception
from aureus.heartbeat.planning import InitiativePlan
from aureus.organs import OrganResponse
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

logger = logging.getLogger(__name__)


class InitiativeTracker:
    """只在心跳线程里记录主动行为，器官任务不持有也不修改这些状态。"""

    def __init__(
        self,
        config: InitiativeConfig,
        timeline: TimelineDatabase,
        feedback: FeedbackStore | None,
        calendar: BudgetCalendar,
    ) -> None:
        self.config = config
        self.state = InitiativeState()
        self.failed = False
        self._timeline = timeline
        self._feedback = feedback
        self._calendar = calendar
        self._day: date | None = None
        self._message: TimelineRecord | None = None
        self._restored = False
        self._blocked_by = ""

    @property
    def enabled(self) -> bool:
        """运行时实际采用的开关，扮演模式和缺规划器时由心跳关闭。"""
        return self.config.enabled

    @property
    def can_consider(self) -> bool:
        """这一拍已恢复历史且没有读写失败时才允许考虑。"""
        return self.enabled and self._restored and not self.failed

    def restore(self, now: datetime) -> None:
        """只恢复消息已有的事实；反馈缺失不重建，首条来信按原窗口判定。"""
        try:
            history = read_initiative_history(self._timeline, self._calendar, now, self.config)
            self.state = history.state
            self._message = history.message
            self._day = self._calendar.day_of(now)
            self._restored = True
            if history.answer_at is not None:
                self.note_user_message(history.answer_at)
        except Exception:
            self.failed = True
            logger.exception("主动联系的历史读不了，这一拍不主动")

    def begin_beat(self, now: datetime) -> None:
        """每拍重置故障标记，跨预算日清次数；未读到历史时再尝试读取。"""
        self.failed = False
        if not self._restored:
            self.restore(now)
        day = self._calendar.day_of(now)
        if self._day != day:
            self.state = replace(self.state, contacts_today=0)
            self._day = day

    def consider(self, decision: InitiativeDecision) -> bool:
        """记录把关结果；同一个拦截原因只记一行日志。"""
        if decision.blocked_by != self._blocked_by:
            self._blocked_by = decision.blocked_by
            if decision.blocked_by:
                logger.debug("这一拍不主动：%s", decision.blocked_by)
        return self.can_consider and decision.allowed

    def start(self, now: datetime) -> None:
        """材料齐全、即将创建后台任务时才开始计算考虑间隔。"""
        self.state = replace(self.state, last_considered_at=now)

    def record_message(
        self, content: str, visibility: VisibilityScope, metadata: dict[str, Any]
    ) -> TimelineRecord:
        """先记齐考虑材料再交回心跳发送；读预期或写消息失败直接交给调用方处理。"""
        confidence = self._feedback.confidence(PROACTIVE_CONTACT).value if self._feedback else None
        return self._timeline.append(
            event_type=EVENT_MESSAGE,
            content=content,
            actor_type=ActorType.ME,
            visibility=visibility,
            metadata={
                **metadata,
                "initiative": True,
                "initiative_expected": confidence if confidence is not None else PRIOR_CONFIDENCE,
            },
        )

    def _note_recorded(self, message: TimelineRecord) -> None:
        """消息已落盘就占次数并等待，发送失败也占用（D78）。"""
        self._message = message
        count = self.state.contacts_today
        day = self._calendar.day_of(message.timestamp)
        if self._day != day:
            count = 0
            self._day = day
        self.state = replace(self.state, contacts_today=count + 1, pending_since=message.timestamp)

    def _record_expectation(self, message: TimelineRecord) -> int | None:
        if self._feedback is not None:
            try:
                return self._feedback.record_expectation(
                    PROACTIVE_CONTACT, message.metadata["initiative_expected"], source_id=message.id
                )
            except Exception:
                self.failed = True
                logger.exception("主动联系的反馈预期没记上，不补记")
        return None

    def record_considered(self, metadata: dict[str, Any]) -> None:
        """每次考虑留一次记录；写失败只记日志，不缓存、不补写。"""
        if metadata.get("failure"):
            self.failed = True
        try:
            self._timeline.append(
                event_type=EVENT_INITIATIVE_CONSIDERED,
                content="考虑主动联系：" + "；".join(metadata["reasons"]),
                actor_type=ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata=metadata,
            )
        except Exception:
            self.failed = True
            logger.exception("主动联系的考虑记录没写进去，不补记")

    def complete(
        self,
        metadata: dict[str, Any],
        plan: InitiativePlan | None,
        response: OrganResponse | None,
        target: Perception | None,
        parts: tuple[str, ...],
        send: Callable[[Perception, TimelineRecord, Callable[[], None], Callable[[], None]], None],
        failure: str,
    ) -> int | None:
        """收器官建议后先记消息、再发出；独立尝试反馈和考虑记录，不缓存失败材料。"""
        metadata = {**metadata, "plan": plan.to_metadata() if plan is not None else None}
        if plan is not None and plan.reach_out and target is None:
            failure = failure or "通道不在了"
        if (
            plan is not None
            and plan.reach_out
            and response is not None
            and target is not None
            and not failure
        ):
            try:
                route = target.route
                route_metadata = (
                    {
                        "account_id": route.account_id,
                        "recipient_id": route.recipient_id,
                        "message_type": route.kind,
                        "temporary_group_id": route.temporary_group_id,
                    }
                    if route is not None
                    else {}
                )
                message = self.record_message(
                    response.suggestion.strip(),
                    target.visibility,
                    {
                        **metadata,
                        "to_user": True,
                        "channel": target.channel,
                        "organ_id": response.organ_id,
                        "reply_segments": list(parts),
                        **route_metadata,
                    },
                )
                metadata["message_id"] = message.id
                self._note_recorded(message)

                def finish(*, sent: bool) -> None:
                    feedback_id = self._record_expectation(message) if sent else None
                    self.record_considered(
                        {
                            **metadata,
                            "decided": "reach_out" if sent else "failed",
                            "sent": sent,
                            "failure": (
                                ("反馈预期没记上" if self.failed else "")
                                if sent
                                else "通道发送失败"
                            ),
                            "feedback_id": feedback_id,
                        }
                    )

                send(target, message, lambda: finish(sent=True), lambda: finish(sent=False))
                return message.id
            except Exception:
                self.failed = True
                logger.exception("主动联系没能发出，这一拍不再主动")
                failure = "主动联系读写状态失败"
        decided = "skip" if plan is not None and not plan.reach_out and not failure else "failed"
        self.record_considered(
            {
                **metadata,
                "decided": decided,
                "sent": False,
                "failure": failure,
                "feedback_id": None,
            }
        )
        return None

    def note_user_message(self, now: datetime) -> None:
        """用户开口立即清退避；有等待时按消息时刻判定，边界算窗口外。"""
        pending = self.state.pending_since
        if pending is not None:
            self._finish(answered=now - pending < timedelta(hours=self.config.answer_window_hours))
        self.state = replace(self.state, unanswered_streak=0)

    def settle_timeout(self, now: datetime) -> None:
        """无人回应且窗口已过时结算一次；写失败也不保留待补材料。"""
        pending = self.state.pending_since
        if pending is not None and now - pending >= timedelta(
            hours=self.config.answer_window_hours
        ):
            self._finish(answered=False)

    def _record_outcome(self, message: TimelineRecord, *, answered: bool) -> None:
        if self._feedback is None:
            return
        try:
            expectation = self._feedback.find_by_source(PROACTIVE_CONTACT, message.id)
            if expectation is not None and expectation.outcome is Outcome.UNKNOWN:
                self._feedback.record_outcome(
                    expectation.id, Outcome.SUCCESS if answered else Outcome.FAILURE
                )
        except Exception:
            self.failed = True
            logger.exception("主动联系的反馈结果没写进去，不补记")

    def _finish(self, *, answered: bool) -> None:
        message = self._message
        if message is None:
            return
        try:
            existing = self._timeline.query(
                TimelineQuery(
                    event_type=EVENT_INITIATIVE_RESULT,
                    metadata={"message_id": message.id},
                    limit=1,
                )
            )
            if not existing:
                self._record_outcome(message, answered=answered)
                self._timeline.append(
                    event_type=EVENT_INITIATIVE_RESULT,
                    content="主动联系后用户回应了。" if answered else "主动联系后一直没有回应。",
                    actor_type=ActorType.ME,
                    visibility=VisibilityScope.PRIVATE,
                    metadata={"message_id": message.id, "answered": answered},
                )
        except Exception:
            self.failed = True
            logger.exception("主动联系的回应结果没写进去，不补记")
        self.state = replace(
            self.state,
            pending_since=None,
            unanswered_streak=0 if answered else self.state.unanswered_streak + 1,
        )
        self._message = None


# D76：有效对话只查最近 50 条自己说的话，不扫描全部经历。
_CONVERSATION_RESTORE_LIMIT = 50


def read_last_conversation(timeline: TimelineDatabase) -> datetime | None:
    """从普通回复恢复社交需求的起点，老回复用所回复的用户消息确认身份（D76）。"""
    records = timeline.query(
        TimelineQuery(
            event_type=EVENT_MESSAGE,
            actor_type=ActorType.ME,
            latest=True,
            limit=_CONVERSATION_RESTORE_LIMIT,
        )
    )
    for record in reversed(records):
        if record.metadata.get("initiative") is True:
            continue
        if "to_user" in record.metadata:
            if record.metadata["to_user"] is True:
                return record.timestamp
            continue
        for record_id in record.metadata.get("in_reply_to", []):
            if isinstance(record_id, int) and not isinstance(record_id, bool):
                source = timeline.get(record_id)
                if source is not None and source.actor_type is ActorType.USER:
                    return record.timestamp
    return None
