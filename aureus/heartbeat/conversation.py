"""从主循环搬来的对话流程；拍里准备材料，后台只调用器官（D80）。"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import tzinfo
from math import hypot, sumprod
from typing import Protocol
from uuid import uuid4

from aureus.agenda import AgendaStore
from aureus.agenda.findings import FindingStore
from aureus.agenda.interests import InterestStore
from aureus.attention import SummaryOutcome, WorkingMemory, route_scope
from aureus.body import Body, BodyReading
from aureus.common import (
    Clock,
    EmpathyConfig,
    HeartbeatConfig,
    JudgeConfig,
    LanguageConfig,
    MemoryConfig,
    PeopleConfig,
)
from aureus.heartbeat import exploration_runner, interest_changes
from aureus.heartbeat.body_summary import ROLEPLAY_NOTE, describe_body_state
from aureus.heartbeat.context import (
    build_judge_context,
    build_judge_prompt,
    build_plan_prompt,
    build_reply_prompt,
    build_system_context,
)
from aureus.heartbeat.diary_runner import DiaryOutcome
from aureus.heartbeat.downtime import Downtime
from aureus.heartbeat.events import (
    EVENT_MESSAGE,
)
from aureus.heartbeat.inbox_attention import InboxSelection
from aureus.heartbeat.inbox_media import InboxMedia
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.judging import SENDER_SUBJECT, Judgment, parse_judgment
from aureus.heartbeat.language_runner import speaker_key
from aureus.heartbeat.memory_consolidation import ConsolidationOutcome
from aureus.heartbeat.people_context import format_people_context
from aureus.heartbeat.perception import (
    Perception,
    TrustLevel,
)
from aureus.heartbeat.persona_growth_runner import PersonaReviewOutcome
from aureus.heartbeat.planning import (
    InitiativePlan,
    Plan,
    ReplyLength,
    clean_text,
    parse_plan,
)
from aureus.heartbeat.recall import MAX_RECALL_CHARS, format_recall, heuristic_keywords
from aureus.heartbeat.reply_strategy import ReplyStrategy, judge_allowed
from aureus.hormones import (
    HormoneEffects,
    Hormones,
    HormoneTrigger,
    make_triggers_from_empathy,
    make_triggers_from_judgment,
)
from aureus.identity import Identity, IdentityLayer
from aureus.memory import LanguageStore, MemoryHit, MemoryIndex
from aureus.mood import MoodReading
from aureus.organs import CallPurpose, OrganCallError, OrganRequest, OrganResponse, join_prompt
from aureus.people import AffinityBook, HistoricalEntry, PeopleDirectory
from aureus.safety import (
    CallRejectedError,
)
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

logger = logging.getLogger(__name__)

_DEFAULT_STRANGER_AFFINITY = PeopleConfig().stranger_affinity
_REVIEW_SENTINEL_PREFIX = "reply_review_"
_MAX_REVIEW_REASON_CHARS = 500


def recent_dialogue(
    timeline: TimelineDatabase, *, visibility: VisibilityScope | None, limit: int
) -> Sequence[TimelineRecord]:
    """取回复上下文中的最近对话，按序号升序返回；网页分隔线也用这份查询（D84）。"""
    return timeline.query(
        TimelineQuery(event_type=EVENT_MESSAGE, visibility=visibility, latest=True, limit=limit)
    )


class OrganCaller(Protocol):
    """心跳调用器官用的对象。核心组装时传入安全网关 SafetyGateway，不传器官总线（D39、D41）。"""

    async def call(self, request: OrganRequest) -> OrganResponse:
        """经过安全检查后调用器官。"""
        ...


def _person_id_of(record: TimelineRecord) -> int | None:
    """这条消息是哪个人发的（人物 ID）；认不出是谁时是 None。"""
    person_id = record.metadata.get("person_id")
    if isinstance(person_id, int) and not isinstance(person_id, bool):
        return person_id
    return None


@dataclass(frozen=True)
class _Heard:
    """已经记进时间线、还没回复的一条消息。"""

    perception: Perception
    record: TimelineRecord
    is_user: bool


@dataclass(frozen=True)
class _ReplyJob:
    """一次回复要用的全部材料。在拍里组装好，后台任务只管按它调用器官，不读也不写状态。"""

    strategy: ReplyStrategy
    # 之前的对话和这次要回复的消息，都是已经记进时间线的记录，从早到晚。
    history: Sequence[TimelineRecord]
    messages: Sequence[TimelineRecord]
    # 怎么称呼用户；后台任务不去读身份，免得它在中途变了。
    user_name: str
    # 规划器和合并成一次的调用看的背景：四层人设都在，由它们决定怎么回应。
    full_context: str
    # 规划之后的回复器看的背景：没有行为风格，什么时候开口、怎么回应已经由规划器定了。
    replyer_context: str
    # 判断器官看的背景：只有不变人格和可变人格（D68 第 6 条）。
    judge_context: str
    # 判断器官要带的之前的对话；已经按 [judge] context_messages 截好。
    judge_history: Sequence[TimelineRecord]
    # 激素影响之前的做法；激素的影响（降长度）每次都从它重新算，不叠加。
    base_strategy: ReplyStrategy
    # 回复的对象：认不出是谁时为 None；对象是不是用户。
    person_id: int | None
    viewer_is_user: bool
    # 把激素的文字（规划器和合并调用用的、回复器用的）放进系统上下文，返回两份新的上下文。
    build_contexts: Callable[[str, str], tuple[str, str]]
    # 此刻激素对这次回复的影响；激素没开时是 None。
    effects: HormoneEffects | None = None
    with_interests: bool = False
    history_ids: tuple[int, ...] = ()
    working_context: str = ""
    people_history: tuple[HistoricalEntry, ...] = ()
    inbox_attention: bool = False
    media_sources: tuple[Perception, ...] = ()
    planner_run: "_PlannerRun | None" = None


@dataclass(frozen=True)
class _JudgeAttempt:
    """一次判断的结果：读懂了就有 judgment，否则 failure 写没判断出来的原因（D68 第 8 条）。"""

    judgment: Judgment | None = None
    failure: str = ""
    # 这次判断引起的激素触发（D69 第 2 条）；后台任务只算出来，由收结果的那一拍写进状态和时间线。
    triggers: tuple[HormoneTrigger, ...] = ()


@dataclass(frozen=True)
class _ThoughtOutcome:
    """一次想法的结果：器官的建议、规划器决定不回复、或者一条要告诉用户的系统通知。"""

    response: OrganResponse | None = None
    notice: str = ""
    # 规划器读懂的建议；没规划、或者看不懂时是 None。
    plan: Plan | None = None
    # 规划器决定不回复：没有 response，也不是失败，不发失败通知。
    declined: bool = False
    # 判断器官的结果；没调用判断（开关关着、档位低、没配器官）时是 None。
    judge: _JudgeAttempt | None = None
    # 这次回复用的激素影响；收结果的那一拍用它记回击次数和不回次数。
    effects: HormoneEffects | None = None
    # 主动联系独立解析，不能拿普通回复的兜底来替它决定开口（D76）。
    initiative_plan: InitiativePlan | None = None
    recall_keywords: tuple[str, ...] = ()
    recall_record_ids: tuple[int, ...] = ()
    recall_source_keys: tuple[str, ...] = ()
    review_reason: str | None = None


@dataclass(frozen=True)
class _InitiativeJob:
    """拍里准备的主动材料，后台只用这些快照调用器官（D76）。"""

    conversation: _ReplyJob
    reasons: tuple[str, ...]
    social_need: float | None
    due_item_ids: tuple[int, ...]
    finding_id: int | None = None


@dataclass(frozen=True)
class _Thought:
    """进行中的想法：后台任务，和它要回复的消息、这次的做法。"""

    task: (
        asyncio.Task[_ThoughtOutcome]
        | asyncio.Task[exploration_runner.ExplorationOutcome]
        | asyncio.Task[DiaryOutcome]
        | asyncio.Task[PersonaReviewOutcome]
        | asyncio.Task[None]
        | asyncio.Task[ConsolidationOutcome]
        | asyncio.Task[InboxSelection]
        | asyncio.Task[SummaryOutcome]
    )
    replying_to: Sequence[_Heard]
    strategy: ReplyStrategy
    # 非空表示主动想法；仍与回复共用唯一任务槽（D34、D76）。
    initiative: _InitiativeJob | None = None
    exploration: TimelineRecord | None = None
    planner_run: "_PlannerRun | None" = None


@dataclass
class _PlannerRun:
    """心跳可取消的首轮规划阶段。"""

    is_planning: bool = False
    restart_requested: bool = False


def _reply_job(batch: Sequence[_Heard], conversation_job: Callable[..., _ReplyJob]) -> _ReplyJob:
    """组装回复要用的材料：做法由精力档位定，人设和醒来信息放系统上下文，对话放提示词。"""
    job = conversation_job(
        batch[-1].perception,
        [heard.record for heard in batch],
        _person_id_of(batch[-1].record),
        viewer_is_user=batch[-1].is_user,
    )
    if any(heard.perception.attachments for heard in batch):
        return replace(job, media_sources=tuple(heard.perception for heard in batch))
    return job


def _organ_request(
    job: _ReplyJob,
    *,
    organ_id: str,
    task: str,
    prompt: str,
    system_context: str,
    purpose: CallPurpose = CallPurpose.USER_REPLY,
) -> OrganRequest:
    """规划和回复共用的请求格式；主动联系显式传 OPTIONAL（D76）。"""
    if job.working_context and task in {"plan", "reply", "initiative_plan", "initiative_reply"}:
        prompt = join_prompt([job.working_context, prompt])
    return OrganRequest(
        organ_id=organ_id,
        prompt=prompt,
        context={"task": task, "in_reply_to": [record.id for record in job.messages]},
        purpose=purpose,
        system_context=system_context,
        model_preference=job.strategy.model_preference,
    )


class ConversationFlow:
    """对话共用的调用依赖；每次的身份和身体材料由拍里显式传入。"""

    def __init__(
        self,
        *,
        clock: Clock,
        timeline: TimelineDatabase,
        config: HeartbeatConfig,
        call: Callable[[OrganRequest], Awaitable[OrganResponse]],
        timezone: tzinfo | None,
        judge: JudgeConfig | None,
        hormones: Hormones | None,
        affinity: AffinityBook | None,
        empathy: EmpathyConfig | None,
        agenda: AgendaStore | None,
        memory: MemoryIndex | None = None,
        memory_config: MemoryConfig | None = None,
        language: LanguageStore | None = None,
        language_config: LanguageConfig | None = None,
        people: PeopleDirectory | None = None,
        people_config: PeopleConfig | None = None,
        working_memory: WorkingMemory | None = None,
    ) -> None:
        self._clock, self._timeline, self._config = clock, timeline, config
        self._call, self._timezone = call, timezone
        self.inbox_media = InboxMedia(call)
        self._judge, self._hormones, self._affinity = judge, hormones, affinity
        self._empathy, self._agenda = empathy, agenda
        self._judge_missing_logged = False
        self._memory = memory
        self._memory_config = memory_config or MemoryConfig()
        self.has_text_embedding = False
        self._language = language
        self._language_config = language_config or LanguageConfig()
        self._people = people
        self._people_config = people_config or PeopleConfig()
        self._working_memory = working_memory

    def _working_context(self, target: Perception, ids: tuple[int, ...]) -> str:
        if self._working_memory is None:
            return ""
        route = target.route
        scope = (
            route_scope(
                target.visibility,
                target.channel,
                route.account_id,
                route.kind,
                route.recipient_id,
                route.temporary_group_id,
            )
            if route is not None
            else None
        )
        return self._working_memory.context(
            owner=target.trust is TrustLevel.VERIFIED_USER
            and target.visibility is VisibilityScope.USER_ONLY,
            route=scope,
            exclude=ids,
        )

    def _conversation_job(
        self,
        target: Perception,
        messages: Sequence[TimelineRecord],
        person_id: int | None,
        *,
        viewer_is_user: bool,
        strategy: ReplyStrategy,
        identity: Identity,
        body: Body | None,
        reading: BodyReading | None,
        downtime: Downtime | None,
        identity_layers: Sequence[IdentityLayer],
        live_identity_changes: list[IdentityLayer],
        interests: InterestStore | None,
        findings: FindingStore | None,
        affinity_for_context: Callable[[int | None], str],
        read_mood: Callable[[int | None], MoodReading | None],
        agenda_for_context: Callable[[int | None], str],
        initiative_note: str = "",
        mood_state: str | None = None,
        agenda_state: str | None = None,
        inbox_attention: bool = False,
    ) -> _ReplyJob:
        """回复和主动联系共用相同的身份、身体、激素、心情和日程背景（D76）。"""
        batch_ids = {record.id for record in messages}
        contact = target.trust is not TrustLevel.VERIFIED_USER
        contact_account = f"{target.channel}:{target.sender_id}" if contact else None
        # 回复用户时只带同一可见范围的对话。回复别人时不按范围过滤（D61，用户决定）：
        # 它的记忆是连续的一份，防套话靠系统上下文里的说明，由规划器和回复器各自判断。
        if target.route is None:
            recent = recent_dialogue(
                self._timeline,
                visibility=None if contact else target.visibility,
                limit=strategy.message_limit,
            )
        else:
            key = ConversationKey.from_perception(target)
            recent = self._timeline.query(
                TimelineQuery(
                    event_type=EVENT_MESSAGE,
                    visibility=target.visibility,
                    latest=True,
                    limit=strategy.message_limit,
                    metadata={
                        "channel": key.platform,
                        "account_id": key.account_id,
                        "message_type": key.kind,
                        "recipient_id": key.recipient_id,
                        "temporary_group_id": key.temporary_group_id,
                    },
                )
            )
        # 按序号排除这次要回复的消息，而不是只取“比它们早的”：想上一件事的时候来的消息，
        # 序号比上一次的回复还小，只取更早的就会漏掉它刚说过的话。
        history = [record for record in recent if record.id not in batch_ids]
        body_state = self._body_state_for_context(strategy, body, reading)
        now = self._clock.now()
        identity_changes = (
            [layer.describe() for layer in identity_layers]
            if self._config.identity_changes_in_prompt
            else []
        )
        live_changes = (
            [layer.describe() for layer in live_identity_changes]
            if self._config.identity_changes_in_prompt
            else []
        )
        live_identity_changes.clear()  # 只在刚改完的这一次回复里说

        affinity_state = affinity_for_context(person_id)
        if mood_state is None:
            mood_reading = read_mood(person_id)
            mood_state = mood_reading.summary if mood_reading is not None else ""
        if agenda_state is None:
            agenda_state = agenda_for_context(person_id)
        interests_state = interest_changes.interest_context(
            interests,
            findings,
            is_user=viewer_is_user,
            visibility=target.visibility,
            timezone=self._timezone,
        )

        def context(*, include_behavior_style: bool, hormone_state: str = "") -> str:
            return build_system_context(
                identity,
                now,
                downtime,
                self._timezone,
                body_state,
                include_behavior_style=include_behavior_style,
                contact_account=contact_account,
                identity_changes=identity_changes,
                identity_changes_live=live_changes,
                hormone_state=hormone_state,
                affinity_state=affinity_state,
                mood_state=mood_state,
                agenda_state=agenda_state,
                interests_state=interests_state,
                initiative_note=initiative_note if not include_behavior_style else "",
            )

        def build_contexts(full_text: str, replyer_text: str) -> tuple[str, str]:
            return (
                context(include_behavior_style=True, hormone_state=full_text),
                context(include_behavior_style=False, hormone_state=replyer_text),
            )

        judge_count = self._judge.context_messages if self._judge is not None else 0
        judge_history = list(history)[-judge_count:] if judge_count > 0 else []
        people_history = (
            tuple(self._people.history_for(person_id, limit=self._memory_config.max_results))
            if messages
            and person_id is not None
            and self._people is not None
            and self._people_config.history_in_reply_enabled
            else ()
        )
        history_ids = tuple(record.id for record in [*history, *messages])
        job = _ReplyJob(
            strategy=strategy,
            history=history,
            messages=messages,
            user_name=identity.user.name,
            full_context=context(include_behavior_style=True),
            replyer_context=context(include_behavior_style=False),
            judge_context=build_judge_context(identity),
            judge_history=judge_history,
            base_strategy=strategy,
            person_id=person_id,
            viewer_is_user=not contact,
            build_contexts=build_contexts,
            with_interests=bool(interests_state),
            history_ids=history_ids,
            working_context=self._working_context(target, history_ids),
            people_history=people_history,
            inbox_attention=inbox_attention,
        )
        return self._apply_hormones(job, ())

    def _apply_hormones(self, job: _ReplyJob, extra: Sequence[HormoneTrigger]) -> _ReplyJob:
        """把激素的影响放进这次回复：摘要和行为说明进上下文，低落时回复长度降一档（D69 第 3 条）。

        Args:
            job: 回复要用的材料；每次都从 base_strategy 重新算，重复调用不会叠加。
            extra: 刚算出、还没正式记下的触发。后台任务不写状态（D41），回复却要带上这次的情绪，
                所以先拿它们临时算，正式写入在收结果的那一拍。
        """
        hormones = self._hormones
        if hormones is None:
            return job
        effects = hormones.effects(
            self._clock.now(),
            person_id=job.person_id,
            viewer_is_user=job.viewer_is_user,
            extra=extra,
        )
        strategy = job.base_strategy
        if effects.shorten_reply:
            current = strategy.length_limit or ReplyLength.LONG
            strategy = replace(strategy, length_limit=current.lowered())
        full_context, replyer_context = job.build_contexts(
            effects.context_text(plan_first=strategy.plan_first),
            effects.context_text(plan_first=False),
        )
        return replace(
            job,
            strategy=strategy,
            full_context=full_context,
            replyer_context=replyer_context,
            effects=effects,
        )

    def _body_state_for_context(
        self, strategy: ReplyStrategy, body: Body | None, reading: BodyReading | None
    ) -> str:
        """放进系统上下文的身体状态摘要；没有读数或开关关着时是空字符串。

        扮演模式下换成固定的提示词，没有任何真实读数（D65）。
        """
        if self._config.roleplay_mode:
            return ROLEPLAY_NOTE
        if body is None or reading is None:
            return ""
        if not body.config.state_in_prompt_enabled:
            return ""
        return describe_body_state(reading, body.config, strategy.describe())

    async def _think(self, job: _ReplyJob) -> _ThoughtOutcome:
        """在后台先规划，明确需要开口才调用回复器。"""
        if job.media_sources:
            messages = await self.inbox_media.enrich(
                job.messages, job.media_sources, job.strategy.model_preference
            )
            job = replace(job, messages=messages)
        attempt = await self._judge_emotion(job)
        if attempt is not None and attempt.triggers:
            job = self._apply_hormones(job, attempt.triggers)
        automatic: tuple[tuple[str, ...], list[MemoryHit]] | None = None
        if self._memory_config.heuristic_recall_enabled and self._can_recall():
            words = heuristic_keywords(job.messages)
            if words and (hits := await self._retrieve(job, words)):
                automatic = words, hits
        try:
            plan = await self._plan(job, automatic)
        except CallRejectedError as rejection:
            return _ThoughtOutcome(
                notice=f"没能调用模型，这次没有回复：{rejection.reason}", judge=attempt
            )
        if job.planner_run is not None and job.planner_run.restart_requested:
            return _ThoughtOutcome(declined=True)
        if plan is None or not plan.should_reply:
            outcome = _ThoughtOutcome(plan=plan, declined=True)
        else:
            outcome = await self._reply_with_review(job, plan, automatic)
        return replace(outcome, judge=attempt, effects=job.effects)

    async def _reply_with_review(
        self,
        job: _ReplyJob,
        plan: Plan,
        automatic: tuple[tuple[str, ...], list[MemoryHit]] | None,
    ) -> _ThoughtOutcome:
        """把有效的驳回理由交给规划器重审一次。"""
        outcome = await self._reply(job, plan, automatic)
        if outcome.review_reason is None:
            return outcome
        if not outcome.review_reason:
            return _ThoughtOutcome(declined=True)
        try:
            revised = await self._plan(job, automatic, review_reason=outcome.review_reason)
        except CallRejectedError:
            return _ThoughtOutcome(declined=True)
        if revised is None or not revised.should_reply:
            return _ThoughtOutcome(declined=True)
        outcome = await self._reply(job, revised, automatic)
        return outcome if outcome.review_reason is None else _ThoughtOutcome(declined=True)

    async def _judge_emotion(self, job: _ReplyJob) -> _JudgeAttempt | None:
        """先调判断器官（D68）。不调用时返回 None；调用了就一定返回结果，任何失败都不影响回复。

        为什么都不抛：判断只是参考，它被网关拒绝不代表回复也会被拒（回复算「回应用户」）。
        """
        config = self._judge
        if config is None or not config.enabled or not judge_allowed(job.strategy.effective_tier):
            return None
        request = OrganRequest(
            organ_id=config.organ,
            prompt=build_judge_prompt(
                job.judge_history, job.messages, job.user_name, self._timezone
            ),
            context={"task": "judge", "in_reply_to": [record.id for record in job.messages]},
            purpose=CallPurpose.OPTIONAL,
            system_context=job.judge_context,
            model_preference=job.strategy.model_preference,
        )
        try:
            async with asyncio.timeout(config.timeout_seconds):
                response = await self._call(request)
        except TimeoutError:
            logger.warning(
                "判断器官 %s 超过 %s 秒没有返回，跳过判断", config.organ, config.timeout_seconds
            )
            return _JudgeAttempt(failure="超时")
        except CallRejectedError as rejection:
            return _JudgeAttempt(failure=f"被安全网关拒绝：{rejection.reason}")
        except OrganCallError as failure:
            logger.warning("判断器官调用失败，跳过判断：%s", failure)
            return _JudgeAttempt(failure="调用失败")
        except ValueError:
            # 器官没注册：用户没配这个器官，不算出错，只提醒一次（D68 第 7 条）。
            if not self._judge_missing_logged:
                self._judge_missing_logged = True
                logger.warning("没有配判断器官 %s，跳过判断", config.organ)
            return None
        except Exception:
            logger.exception("调用判断器官 %s 时出错，跳过判断", config.organ)
            return _JudgeAttempt(failure="调用出错")
        judgment = parse_judgment(response.suggestion)
        if judgment is None:
            logger.warning("判断器官的建议看不懂，跳过判断")
            return _JudgeAttempt(failure="返回的内容看不懂")
        return _JudgeAttempt(judgment=judgment, triggers=self._triggers_from(judgment, job))

    def _triggers_from(self, judgment: Judgment, job: _ReplyJob) -> tuple[HormoneTrigger, ...]:
        """判断结果变成激素触发（D69 第 2 条）。只算不写；认不出是谁发的就不触发。"""
        hormones = self._hormones
        latest = job.messages[-1]
        person_id = job.person_id
        if hormones is None or person_id is None:
            return ()
        now = self._clock.now()
        direct = make_triggers_from_judgment(
            judgment.toward_me,
            person_id=person_id,
            source_record_id=latest.id,
            source_visibility=latest.visibility,
            now=now,
            config=hormones.config,
            recent_triggers=hormones.triggers,
        )
        empathic: list[HormoneTrigger] = []
        if self._empathy is not None:
            empathic = make_triggers_from_empathy(
                judgment.others,
                person_id=person_id,
                affinity_of=lambda subject: self._affinity_of(subject, person_id),
                source_record_id=latest.id,
                source_visibility=latest.visibility,
                now=now,
                config=hormones.config,
                empathy=self._empathy,
                # 同一批里先算出的直接触发也算进去，免得同一条消息重复数
                recent_triggers=[*hormones.triggers, *direct],
            )
        return (*direct, *empathic)

    def _affinity_of(self, subject: str, sender_id: int) -> float:
        """共情对象的好感（D72 第 2 条）。

        发消息的人取他的好感；消息里提到的人或动物没有档案，好感账关着时，都按陌生人默认值。
        """
        if self._affinity is None:
            return _DEFAULT_STRANGER_AFFINITY
        if subject == SENDER_SUBJECT:
            return self._affinity.current(sender_id)
        return self._affinity.config.stranger_affinity

    async def _plan(
        self,
        job: _ReplyJob,
        automatic: tuple[tuple[str, ...], list[MemoryHit]] | None,
        *,
        review_reason: str = "",
    ) -> Plan | None:
        """调规划器，返回读懂的建议。调用失败或看不懂时返回 None（记日志）；网关的拒绝照常抛出。"""
        organ_id = self._config.plan_organ
        heuristic_recall = (
            format_recall(*automatic, self._timezone, heuristic=True)
            if automatic is not None
            else ""
        )
        prompt = build_plan_prompt(
            job.history,
            job.messages,
            job.user_name,
            self._timezone,
            with_agenda=self._agenda is not None,
            with_interests=job.with_interests,
            with_recall=self._can_recall(),
            jargon=await self._jargon_reference(job),
            with_expressions=self._can_recall_language(),
            heuristic_recall=heuristic_recall,
            review_reason=review_reason,
        )
        if job.inbox_attention:
            inbox_note = (
                "如想在空闲时翻阅平台收件箱，可在 JSON 中加 read_inbox: true。"
                "如果经过这次交谈，你想将当前已认识的联系人设为特别关心，"
                "可加 special_contact: true；"
                "想取消可写 false。特别关心的人来信会叫醒你。此选择由你作出。"
            )
            if any("inbox_message_id" in record.metadata for record in job.messages):
                inbox_note += (
                    "想留在本会话持续聊天，在 JSON 中加 stay_in_conversation: true；"
                    "想退出请写 false。"
                )
            prompt = join_prompt([prompt, inbox_note])
        request = _organ_request(
            job,
            organ_id=organ_id,
            task="plan",
            prompt=prompt,
            system_context=job.full_context,
        )
        request = replace(request, stream=self._config.inbox_debounce_enabled)
        run = job.planner_run if not review_reason else None
        if run is not None:
            run.is_planning = True
        try:
            response = await self._call(request)
        except CallRejectedError:
            raise
        except OrganCallError as failure:
            logger.warning("规划器调用失败：%s", failure)
            return None
        except Exception:
            logger.exception("调用规划器 %s 时出错", organ_id)
            return None
        finally:
            if run is not None:
                run.is_planning = False
        plan = parse_plan(response.suggestion)
        if plan is None:
            logger.warning("规划器的建议看不懂")
        return plan

    def _can_recall(self) -> bool:
        return (
            self._memory is not None
            and self._memory_config.enabled
            and self._memory_config.recall_enabled
        )

    def _can_recall_language(self) -> bool:
        return (
            self._language is not None
            and self._language_config.recall_enabled
            and self._language_config.reference_chars > 0
        )

    async def _jargon_reference(self, job: _ReplyJob) -> str:
        if self._language is None or not self._can_recall_language():
            return ""
        texts = [
            record.content
            for record in reversed([*job.history, *job.messages])
            if record.actor_type in (ActorType.USER, ActorType.PERSON)
        ]
        try:
            return await asyncio.to_thread(
                self._language.jargon_reference, texts, self._language_config.reference_chars
            )
        except sqlite3.Error:
            logger.exception("读取黑话参考失败")
            return ""

    async def _expression_reference(self, job: _ReplyJob, plan: Plan | None) -> str:
        if self._language is None or not self._can_recall_language():
            return ""
        topic = "\n".join(record.content for record in job.messages)
        intent = " ".join((plan.intent, *plan.expression_focus)) if plan is not None else ""
        avoid = plan.expression_avoid if plan is not None else ()
        try:
            return await asyncio.to_thread(
                self._language.expression_reference,
                topic,
                intent,
                avoid,
                speaker_key(job.messages[-1]),
                self._language_config.reference_chars,
            )
        except sqlite3.Error:
            logger.exception("读取习得表达参考失败")
            return ""

    async def _retrieve(self, job: _ReplyJob, words: tuple[str, ...]) -> list[MemoryHit] | None:
        """后台读取有上限的检索结果，保留查询失败与无命中的区别。"""
        if self._memory is None:
            return None
        try:
            person_keys = None
            if "我说过" in job.messages[-1].content:
                if job.person_id is None or self._people is None:
                    return []
                person_keys = self._people.memory_person_keys(job.person_id)
            limit = self._memory_config.max_results
            hits = await asyncio.to_thread(
                self._memory.search,
                words,
                job.history_ids,
                limit * 3 if self.has_text_embedding else limit,
                person_key=person_keys,
                include_user_extractions=(
                    job.viewer_is_user and job.messages[-1].visibility is VisibilityScope.USER_ONLY
                ),
            )
            if self.has_text_embedding and len(hits) > 1:
                hits = await self._rerank_memory(words, hits)
            return hits[:limit]
        except Exception:
            logger.exception("记忆检索失败，这次检索不附记忆")
            return None

    async def _rerank_memory(
        self, words: tuple[str, ...], hits: list[MemoryHit]
    ) -> list[MemoryHit]:
        """只给已经通过现有检索过滤的候选重新排序。"""
        try:
            response = await self._call(
                OrganRequest(
                    "text_embedding",
                    "",
                    purpose=CallPurpose.OPTIONAL,
                    embedding_inputs=(" ".join(words), *(hit.text for hit in hits)),
                )
            )
        except (CallRejectedError, OrganCallError):
            return hits
        if len(response.embeddings) != len(hits) + 1:
            return hits
        query, *candidates = response.embeddings
        query_norm = hypot(*query)
        if not query_norm:
            return hits

        def similarity(item: tuple[MemoryHit, tuple[float, ...]]) -> float:
            vector = item[1]
            norm = hypot(*vector)
            return sumprod(query, vector) / query_norm / norm if norm else 0.0

        return [
            hit
            for hit, _ in sorted(zip(hits, candidates, strict=True), key=similarity, reverse=True)
        ]

    async def _reply(
        self,
        job: _ReplyJob,
        plan: Plan,
        automatic: tuple[tuple[str, ...], list[MemoryHit]] | None,
    ) -> _ThoughtOutcome:
        """让回复器按规划写成发言，或通过内部标记驳回。"""
        strategy = job.strategy
        length = (
            plan.length.capped_at(strategy.length_limit)
            if plan.length is not None
            else strategy.length_limit
        )
        sentinel = _REVIEW_SENTINEL_PREFIX + uuid4().hex
        recall_text = ""
        keywords: tuple[str, ...] = ()
        hits: list[MemoryHit] = []
        if plan.recall and self._can_recall():
            if (explicit := await self._retrieve(job, plan.recall)) is not None:
                hits = explicit
                keywords = plan.recall
                recall_text = format_recall(keywords, hits, self._timezone)
        elif automatic is not None:
            keywords, hits = automatic
            recall_text = format_recall(keywords, hits, self._timezone, heuristic=True)
        request = _organ_request(
            job,
            organ_id=self._config.reply_organ,
            task="reply",
            prompt=build_reply_prompt(
                job.history,
                job.messages,
                job.user_name,
                self._timezone,
                intent=plan.intent,
                plan_reason=plan.reason,
                review_sentinel=sentinel,
                length=length,
                recall=recall_text,
                expressions=await self._expression_reference(job, plan),
                people_context=(
                    format_people_context(
                        job.people_history,
                        job.person_id,
                        MAX_RECALL_CHARS - len(recall_text) - (2 if recall_text else 0),
                    )
                    if job.person_id is not None
                    else ""
                ),
            ),
            system_context=job.replyer_context,
        )
        try:
            response = await self._call(request)
        except CallRejectedError as rejection:
            return _ThoughtOutcome(notice=f"没能调用模型，这次没有回复：{rejection.reason}")
        except OrganCallError as failure:
            return _ThoughtOutcome(notice=f"模型调用失败，这次没有回复。{failure}")
        except Exception as error:
            # 为什么捕获所有异常：后台任务里的异常没人接住，用户只会看到它一直不说话。
            logger.exception("调用器官 %s 时出错", request.organ_id)
            return _ThoughtOutcome(
                notice=f"调用模型时出错（{type(error).__name__}），这次没有回复，详情见日志。"
            )
        text = response.suggestion.strip()
        if not text:
            return _ThoughtOutcome(notice="模型返回了空回复，这次没有回复。")
        if _REVIEW_SENTINEL_PREFIX in text or "{sentinel}" in text:
            opening, closing = f"<{sentinel}>", f"</{sentinel}>"
            reason = ""
            if text.startswith(opening) and text.endswith(closing):
                body = text[len(opening) : -len(closing)].removesuffix("\\")
                if _REVIEW_SENTINEL_PREFIX not in body and "{sentinel}" not in body:
                    reason = clean_text(body, _MAX_REVIEW_REASON_CHARS)
            return _ThoughtOutcome(review_reason=reason)
        return _ThoughtOutcome(
            response=response,
            plan=plan,
            recall_keywords=keywords,
            recall_record_ids=tuple(hit.record_id for hit in hits if hit.record_id is not None),
            recall_source_keys=tuple(hit.source.encode() for hit in hits if hit.source is not None),
        )
