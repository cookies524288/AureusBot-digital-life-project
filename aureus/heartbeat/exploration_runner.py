"""兴趣建议的应用与小探索状态；状态写入只在心跳收结果时执行（D79）。"""

import logging
from datetime import datetime, tzinfo

from aureus.agenda.findings import FindingStore
from aureus.agenda.interests import InterestStore
from aureus.common import BudgetCalendar
from aureus.heartbeat.context import describe_interests
from aureus.heartbeat.events import EVENT_INTEREST_CHANGED
from aureus.heartbeat.perception import Perception, TrustLevel
from aureus.heartbeat.planning import InterestAction, InterestChange
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

logger = logging.getLogger(__name__)


class ExplorationTracker:
    """心跳持有的兴趣与探索接口，不交给后台器官读写。"""

    def __init__(
        self,
        timeline: TimelineDatabase,
        calendar: BudgetCalendar,
        interests: InterestStore | None = None,
        findings: FindingStore | None = None,
    ) -> None:
        self.timeline, self.calendar, self.interests, self.findings = (
            timeline,
            calendar,
            interests,
            findings,
        )

    def context(self, target: Perception, timezone: tzinfo | None) -> str:
        """兴趣关着、群聊或非用户对话时完全不注入兴趣信息。"""
        if (
            self.interests is None
            or not self.interests.config.enabled
            or target.trust is not TrustLevel.VERIFIED_USER
            or target.visibility is not VisibilityScope.USER_ONLY
        ):
            return ""
        try:
            return describe_interests(
                self.interests.active(),
                self.findings.recent(2) if self.findings is not None else [],
                timezone,
            )
        except Exception:
            logger.exception("兴趣上下文读不了")
            return ""

    def apply_interest(
        self, change: InterestChange | None, source: TimelineRecord, now: datetime
    ) -> None:
        """只从用户档案的私聊应用建议；表或留痕失败只记日志，不补记。"""
        store = self.interests
        if (
            store is None
            or not store.config.enabled
            or change is None
            or source.actor_type is not ActorType.USER
            or source.visibility is not VisibilityScope.USER_ONLY
            or source.metadata.get("person_id") is None
            or store.config.max_changes_per_day == 0
        ):
            return
        try:
            records = self.timeline.query(
                TimelineQuery(
                    event_type=EVENT_INTEREST_CHANGED,
                    actor_type=ActorType.ME,
                    latest=True,
                    limit=store.config.max_changes_per_day,
                )
            )
            if (
                sum(self.calendar.day_of(r.timestamp) == self.calendar.day_of(now) for r in records)
                >= store.config.max_changes_per_day
            ):
                return
            if change.action is InterestAction.ADD:
                item = store.add(change.text or "", "self", source.id)
            elif change.action is InterestAction.REMOVE:
                item = store.remove(change.interest_id or 0, "self_removed")
            else:
                item = store.replace(change.interest_id or 0, change.text or "", source.id)
            if item is not None:
                self.timeline.append(
                    EVENT_INTEREST_CHANGED,
                    f"兴趣改动：{item.text}",
                    ActorType.ME,
                    visibility=VisibilityScope.PRIVATE,
                    metadata={
                        "action": change.action.value,
                        "interest_id": change.interest_id or item.id,
                        "new_interest_id": item.id
                        if change.action is not InterestAction.REMOVE
                        else None,
                        "text": item.text,
                        "reason": change.reason,
                        "source_record_id": source.id,
                    },
                )
        except Exception:
            logger.exception("兴趣改动或留痕失败，不补记")
