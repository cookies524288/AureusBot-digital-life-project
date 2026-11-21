"""人设成长的每日状态、后台建议与拍内落地（D81）；失败只少做，不补记。"""

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime

from aureus.body import EnergyTier
from aureus.common import BudgetCalendar, Clock, DiaryConfig, PersonaGrowthConfig
from aureus.heartbeat import events
from aureus.heartbeat.context import build_system_context
from aureus.heartbeat.persona_growth import (
    PersonaSuggestion,
    build_prompt,
    check_suggestion,
    parse_suggestion,
    read_material,
    should_review,
)
from aureus.identity import Identity, IdentityHistory, IdentityHistoryError
from aureus.organs import CallPurpose, OrganRequest, OrganResponse
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

logger = logging.getLogger(__name__)
_RESULT_EVENTS = (
    events.EVENT_PERSONA_CHANGED,
    events.EVENT_PERSONA_UNCHANGED,
    events.EVENT_PERSONA_REVIEW_FAILED,
)


@dataclass(frozen=True)
class PersonaReviewJob:
    """后台只读的器官请求和这次材料可引用的序号。"""

    day: date
    request: OrganRequest
    material_ids: frozenset[int]


@dataclass(frozen=True)
class PersonaReviewOutcome:
    """器官给出的建议或失败，尚未写入任何状态。"""

    day: date
    suggestion: PersonaSuggestion | None = None
    failure: str | None = None


class PersonaGrowthTracker:
    """每个预算日最多回顾一次；单任务槽使引用集合只对应当前请求。"""

    def __init__(
        self,
        timeline: TimelineDatabase,
        history: IdentityHistory,
        config: PersonaGrowthConfig,
        calendar: BudgetCalendar,
        has_organ: bool,
        clock: Clock,
        *,
        diary_config: DiaryConfig,
    ) -> None:
        self.timeline, self.history, self.config = timeline, history, config
        self.calendar, self.has_organ, self.clock = calendar, has_organ, clock
        self.diary_config = diary_config
        self.attempted_day: date | None = None
        # 来自 prepare 保留下来的当次材料；finish 只接受这些序号。
        self._material_ids: frozenset[int] = frozenset()
        if config.enabled and not has_organ:
            logger.warning("没有总结器官，不做人设回顾")

    def restore(self, now: datetime) -> None:
        """今天的三类结果任一存在即算做过；读取失败也不再尝试。"""
        if not self.config.enabled:
            return
        day = self.calendar.day_of(now)
        try:
            if any(
                record.metadata.get("day") == day.isoformat()
                for kind in _RESULT_EVENTS
                for record in self.timeline.query(
                    TimelineQuery(event_type=kind, latest=True, limit=1)
                )
            ):
                self.attempted_day = day
        except sqlite3.Error:
            self.attempted_day = day
            logger.exception("人设回顾历史读不了，今天不再回顾")

    def prepare(
        self,
        now: datetime,
        identity: Identity,
        *,
        diary_attempted_today: bool,
        tier: EnergyTier | None,
        halted: bool,
        busy: bool,
    ) -> PersonaReviewJob | None:
        """拍里先占当天次数，再取可信材料；空材料不花一次器官调用。"""
        day = self.calendar.day_of(now)
        if not should_review(
            self.config,
            now=now,
            calendar=self.calendar,
            diary_config=self.diary_config,
            diary_attempted_today=diary_attempted_today,
            attempted_today=self.attempted_day == day,
            has_organ=self.has_organ,
            busy=busy,
            halted=halted,
            tier=tier,
        ):
            return None
        self.attempted_day = day
        try:
            material = read_material(self.timeline, self.calendar, now, self.config.max_input_chars)
            if not material:
                self._record(events.EVENT_PERSONA_UNCHANGED, day, "今天没有和用户的经历")
                return None
            self._material_ids = frozenset(line.record_id for line in material)
            request = OrganRequest(
                self.config.organ,
                build_prompt(material, identity),
                system_context=build_system_context(identity, now, None, self.calendar.timezone),
                purpose=CallPurpose.OPTIONAL,
                context={"task": "persona_review"},
            )
            return PersonaReviewJob(day, request, self._material_ids)
        except sqlite3.Error:
            logger.exception("人设回顾材料读不了，今天不再回顾")
            return None

    def _record(self, event: str, day: date, message: str, **metadata: object) -> bool:
        """留痕成功才允许后续改版本；写失败只记日志。"""
        try:
            self.timeline.append(
                event,
                message,
                ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata={"day": day.isoformat(), **metadata},
            )
        except sqlite3.Error:
            logger.exception("人设回顾时间线写失败，不补记")
            return False
        return True

    def finish(self, outcome: PersonaReviewOutcome, identity: Identity) -> Identity | None:
        """用此刻身份重新把关，先留痕再记版本；中途失败不改变生效身份。"""
        day, suggestion = outcome.day, outcome.suggestion
        if outcome.failure is not None:
            self._record(
                events.EVENT_PERSONA_REVIEW_FAILED, day, "人设回顾失败", failure=outcome.failure
            )
            return None
        if suggestion is None:
            self._record(events.EVENT_PERSONA_UNCHANGED, day, "今天的人设不变")
            return None
        try:
            previous = self.history.latest_self_change(suggestion.layer)
            rejected = check_suggestion(
                suggestion,
                current=identity,
                locks=identity.persona.locks,
                last_self_change_at=previous.created_at if previous is not None else None,
                now=self.clock.now(),
                material_ids=self._material_ids,
                config=self.config,
            )
            if rejected is not None:
                self._record(
                    events.EVENT_PERSONA_UNCHANGED,
                    day,
                    "这次人设建议没有采纳",
                    rejected=rejected.value,
                    layer=suggestion.layer.value,
                )
                return None
            if not self._record(
                events.EVENT_PERSONA_CHANGED,
                day,
                f"我变了：{suggestion.layer.describe()}。因为{suggestion.reason}",
                layer=suggestion.layer.value,
                content=suggestion.content,
                reason=suggestion.reason,
                evidence=list(suggestion.evidence),
            ):
                return None
            self.history.record_self_change(suggestion.layer, suggestion.content)
        except (IdentityHistoryError, sqlite3.Error):
            logger.exception("人设版本写失败或历史读不了，不改人设、不补记")
            return None
        persona = identity.persona.model_copy(update={suggestion.layer.value: suggestion.content})
        return identity.model_copy(update={"persona": persona})


async def review_persona(
    job: PersonaReviewJob,
    call: Callable[[OrganRequest], Awaitable[OrganResponse]],
) -> PersonaReviewOutcome:
    """后台只调用器官和解析建议；取消照常向上传递，不写结果。"""
    try:
        response = await call(job.request)
    except Exception:
        logger.exception("人设回顾器官调用失败")
        return PersonaReviewOutcome(job.day, failure="organ_failed")
    suggestion = parse_suggestion(response.suggestion)
    if suggestion == "bad":
        return PersonaReviewOutcome(job.day, failure="bad_output")
    return PersonaReviewOutcome(job.day, suggestion)
