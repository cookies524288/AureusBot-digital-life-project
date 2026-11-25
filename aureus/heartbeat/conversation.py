"""从主循环搬来的对话流程；拍里准备材料，后台只调用器官（D80）。"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, tzinfo
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
from aureus.heartbeat.body_summary import ROLEPLAY_NOTE, describe_body_state, describe_judge_body
from aureus.heartbeat.context import (
    build_judge_context,
    build_judge_prompt,
    build_plan_prompt,
    build_reply_request,
    build_system_context,
)
from aureus.heartbeat.diary_runner import DiaryOutcome
from aureus.heartbeat.downtime import Downtime
from aureus.heartbeat.emoji_selection import EMOJI_CONTEXT_LIMIT, EmojiSelector
from aureus.heartbeat.empathy import EmpathyFocus
from aureus.heartbeat.events import (
    EVENT_MESSAGE,
)
from aureus.heartbeat.expression_recall import ExpressionRecall
from aureus.heartbeat.history_retention import CleanupPreview, HistoryRetention
from aureus.heartbeat.inbox_attention import InboxResidence, InboxSelection
from aureus.heartbeat.inbox_media import InboxMedia
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.judging import SENDER_SUBJECT, Judgment, parse_judgment
from aureus.heartbeat.language_runner import local_language_scope, speaker_key
from aureus.heartbeat.memory_consolidation import ConsolidationOutcome
from aureus.heartbeat.memory_summary_source import summary_scope_for
from aureus.heartbeat.people_context import format_people_context
from aureus.heartbeat.perception import (
    Perception,
    TrustLevel,
)
from aureus.heartbeat.persona_growth_runner import PersonaReviewOutcome
from aureus.heartbeat.planner_tools import (
    PlannerAction,
    PlannerToolOptions,
    dispatch_planner_tools,
    planner_tools,
)
from aureus.heartbeat.planning import (
    InitiativePlan,
    Plan,
    ReplyLength,
    clean_text,
)
from aureus.heartbeat.platform_history import HistoryQuery, PlatformHistory
from aureus.heartbeat.recall import (
    MAX_RECALL_CHARS,
    format_recall_hits,
    heuristic_keywords,
)
from aureus.heartbeat.reply_outbox import EmojiReplyPart
from aureus.heartbeat.reply_segments import ReplyDraft, prepare_reply
from aureus.heartbeat.reply_strategy import ReplyStrategy, judge_allowed
from aureus.hormones import (
    HormoneEffects,
    Hormones,
    HormoneTrigger,
    kind_enabled,
    make_triggers_from_empathy,
    make_triggers_from_judgment,
)
from aureus.identity import Identity, IdentityLayer
from aureus.memory import EmojiStore, ExpressionVectorIndex, LanguageStore, MemoryHit, MemoryIndex
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
type _Recall = tuple[tuple[str, ...], Sequence[MemoryHit]]


class _SummaryRecallChangedError(Exception):
    """本轮器官已接触的派生事实不再可用。"""


@dataclass
class _RoundRecall:
    """只跟踪本轮提示词中实际交给规划器的派生事实。"""

    automatic: _Recall | None = None
    seen: list[MemoryHit] = field(default_factory=list)


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


def _same_judge_source(record: TimelineRecord, target: Perception) -> bool:
    """判断用的历史只取同一个会话，不使用工作记忆中的跨会话材料。"""
    metadata, route = record.metadata, target.route
    if record.visibility is not target.visibility or metadata.get("channel") != target.channel:
        return False
    if route is None:
        return metadata.get("account_id") is None
    return (
        metadata.get("account_id") == route.account_id
        and metadata.get("recipient_id") == route.recipient_id
        and metadata.get("message_type") == route.kind
        and metadata.get("temporary_group_id") == route.temporary_group_id
    )


@dataclass(frozen=True)
class _Heard:
    """已经记进时间线、还没回复的一条消息。"""

    perception: Perception
    record: TimelineRecord
    is_user: bool
    residence: InboxResidence | None = None


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
    # 判断器官看的背景：两层人格和允许附加的本轮自身读数（D68、3.15）。
    judge_context: str
    # 判断器官要带的之前的对话；已经按 [judge] context_messages 截好。
    judge_history: Sequence[TimelineRecord]
    # 激素影响之前的做法；激素的影响（降长度）每次都从它重新算，不叠加。
    base_strategy: ReplyStrategy
    # 回复的对象：认不出是谁时为 None；对象是不是用户。
    person_id: int | None
    viewer_is_user: bool
    # 把激素的文字（规划器和合并调用用的、回复器用的）放进系统上下文，返回两份新的上下文。
    build_contexts: Callable[[str, str, str | None], tuple[str, str]]
    read_mood: Callable[[datetime, Sequence[HormoneTrigger]], MoodReading] | None = None
    mood_reading: MoodReading | None = None
    empathy_detail_for_all: bool = False
    # 此刻激素对这次回复的影响；激素没开时是 None。
    effects: HormoneEffects | None = None
    with_interests: bool = False
    history_ids: tuple[int, ...] = ()
    working_context: str = ""
    people_history: tuple[HistoricalEntry, ...] = ()
    inbox_attention: bool = False
    media_sources: tuple[Perception, ...] = ()
    planner_run: "_PlannerRun | None" = None
    emoji_history: Sequence[TimelineRecord] | None = None
    language_scope_key: str | None = None
    summary_scope: tuple[str, str] | None = None
    legacy_user_extractions: bool = False
    can_quote: bool = False
    history_route: ConversationKey | None = None
    history_visibility: VisibilityScope = VisibilityScope.PRIVATE
    round_trace: "_RoundTrace | None" = None


@dataclass(frozen=True)
class _JudgeAttempt:
    """一次判断的结果：读懂了就有 judgment，否则 failure 写没判断出来的原因（D68 第 8 条）。"""

    judgment: Judgment | None = None
    failure: str = ""
    # 这次判断引起的激素触发（D69 第 2 条）；后台任务只算出来，由收结果的那一拍写进状态和时间线。
    triggers: tuple[HormoneTrigger, ...] = ()


@dataclass
class _RoundTrace:
    """真实收件批次的首条记录序号，以及供收结果时追加的可展示证据。"""

    id: int
    actions: list[PlannerAction] = field(default_factory=list)
    judge: _JudgeAttempt | None = None
    cleanup_preview: CleanupPreview | None = None
    used_platform_history: bool = False


def _trace_action(job: _ReplyJob, action: PlannerAction) -> None:
    if job.round_trace is not None:
        job.round_trace.actions.append(action)


@dataclass(frozen=True)
class _ThoughtOutcome:
    """一次想法的结果：器官的建议、规划器决定不回复、或者一条要告诉用户的系统通知。"""

    response: OrganResponse | None = None
    notice: str = ""
    # 规划器读懂的建议；没规划、或者看不懂时是 None。
    plan: Plan | None = None
    reference_plan: Plan | None = None
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
    recall_feedback_refs: tuple[str, ...] = ()
    review_reason: str | None = None
    reply_segments: tuple[str, ...] = ()
    emoji: EmojiReplyPart | None = None


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
        | asyncio.Task[bool]
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
    round_trace: _RoundTrace | None = None
    inbox_discard_reason: str | None = None


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
    if job.working_context and task in {"plan", "initiative_plan", "initiative_reply"}:
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
        empathy_focus: EmpathyFocus,
        agenda: AgendaStore | None,
        memory: MemoryIndex | None = None,
        emoji_store: EmojiStore | None = None,
        memory_config: MemoryConfig | None = None,
        language: LanguageStore | None = None,
        language_config: LanguageConfig | None = None,
        expression_index: ExpressionVectorIndex | None = None,
        people: PeopleDirectory | None = None,
        people_config: PeopleConfig | None = None,
        working_memory: WorkingMemory | None = None,
        platform_history: PlatformHistory | None = None,
        history_retention: HistoryRetention | None = None,
        inbox_store: InboxStore | None = None,
    ) -> None:
        self._clock, self._timeline, self._config = clock, timeline, config
        self._call, self._timezone = call, timezone
        self.inbox_media = InboxMedia(
            call, emoji_store if config.qq_emoji_collection_enabled else None, inbox_store
        )
        self._judge, self._hormones, self._affinity = judge, hormones, affinity
        self._empathy, self._empathy_focus, self._agenda = empathy, empathy_focus, agenda
        self._judge_missing_logged = False
        self._memory = memory
        self.emoji_selector = (
            EmojiSelector(emoji_store, call, clock, config, timezone)
            if config.qq_emoji_selection_enabled and emoji_store is not None
            else None
        )
        self._memory_config = memory_config or MemoryConfig()
        self.has_text_embedding = False
        self.has_fast_model = False
        self._language = language
        self._language_config = language_config or LanguageConfig()
        self.expression_recall = (
            ExpressionRecall(language, expression_index, self._language_config, call)
            if language is not None and expression_index is not None
            else None
        )
        self._people = people
        self._people_config = people_config or PeopleConfig()
        self._working_memory = working_memory
        self.platform_history = platform_history
        self.history_retention = history_retention

    def _working_context(
        self, memory: WorkingMemory, target: Perception, batch_ids: set[int], limit: int
    ) -> tuple[Sequence[TimelineRecord], str, tuple[int, ...]]:
        """用同一份近期焦点组装对话历史、其他经历与摘要（D96）。"""
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
        owner = (
            target.trust is TrustLevel.VERIFIED_USER
            and target.visibility is VisibilityScope.USER_ONLY
        )
        focus = memory.dialogue_focus(owner=owner, route=scope, exclude=batch_ids)
        history = [record for record in focus if record.event_type == EVENT_MESSAGE][-limit:]
        experiences = [record for record in focus if record.event_type != EVENT_MESSAGE]
        material = memory.describe_context(owner=owner, route=scope, focus=experiences)
        return history, material, tuple(record.id for record in experiences)

    def _recent_history(self, target: Perception, limit: int) -> Sequence[TimelineRecord]:
        """停用工作记忆时沿用按精力档位查询对话的规则（D59、D61、D84）。"""
        if target.route is None:
            if (
                target.visibility is VisibilityScope.USER_ONLY
                and target.trust is not TrustLevel.VERIFIED_USER
            ):
                return ()
            return recent_dialogue(
                self._timeline,
                visibility=target.visibility,
                limit=limit,
            )
        key = ConversationKey.from_perception(target)
        return self._timeline.query(
            TimelineQuery(
                event_type=EVENT_MESSAGE,
                visibility=target.visibility,
                latest=True,
                limit=limit,
                metadata={
                    "channel": key.platform,
                    "account_id": key.account_id,
                    "message_type": key.kind,
                    "recipient_id": key.recipient_id,
                    "temporary_group_id": key.temporary_group_id,
                },
            )
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
        read_mood: Callable[[datetime, Sequence[HormoneTrigger]], MoodReading] | None,
        agenda_for_context: Callable[[int | None], str],
        initiative_note: str = "",
        mood_state: str | None = None,
        agenda_state: str | None = None,
        inbox_attention: bool = False,
        can_quote: bool = False,
    ) -> _ReplyJob:
        """回复和主动联系共用相同的身份、身体、激素、心情和日程背景（D76）。"""
        batch_ids = {record.id for record in messages}
        contact = target.trust is not TrustLevel.VERIFIED_USER
        contact_account = f"{target.channel}:{target.sender_id}" if contact else None
        memory = self._working_memory
        if memory is not None and memory.config.working_memory_enabled and memory.is_ready:
            history, working_context, experience_ids = self._working_context(
                memory, target, batch_ids, strategy.message_limit
            )
        else:
            recent = self._recent_history(target, strategy.message_limit)
            history = [record for record in recent if record.id not in batch_ids]
            working_context, experience_ids = "", ()
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
        if agenda_state is None:
            agenda_state = agenda_for_context(person_id)
        interests_state = interest_changes.interest_context(
            interests,
            findings,
            is_user=viewer_is_user,
            visibility=target.visibility,
            timezone=self._timezone,
        )

        def context(
            *, include_behavior_style: bool, hormone_state: str = "", mood_summary: str = ""
        ) -> str:
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
                mood_state=mood_summary,
                agenda_state=agenda_state,
                interests_state=interests_state,
                initiative_note=initiative_note if not include_behavior_style else "",
            )

        def build_contexts(
            full_text: str, replyer_text: str, mood_summary: str | None
        ) -> tuple[str, str]:
            summary = mood_summary if mood_summary is not None else mood_state or ""
            return (
                context(include_behavior_style=True, hormone_state=full_text, mood_summary=summary),
                context(
                    include_behavior_style=False, hormone_state=replyer_text, mood_summary=summary
                ),
            )

        judge_count = self._judge.context_messages if self._judge is not None else 0
        judge_history = (
            [record for record in history if _same_judge_source(record, target)][-judge_count:]
            if judge_count > 0
            else []
        )
        people_history = (
            tuple(
                self._people.history_for(
                    person_id,
                    limit=self._memory_config.max_results,
                    use_local_aliases=self._people_config.profile_alias_maintenance_in_reply_enabled,
                    use_local_content=self._people_config.profile_content_maintenance_in_reply_enabled,
                )
            )
            if messages
            and person_id is not None
            and self._people is not None
            and self._people_config.history_in_reply_enabled
            else ()
        )
        history_ids = (*experience_ids, *(record.id for record in [*history, *messages]))
        history_route = ConversationKey.from_perception(target) if target.route else None
        route_key = history_route.language_scope_key() if history_route is not None else None
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
            read_mood=read_mood,
            empathy_detail_for_all=viewer_is_user
            and target.visibility is VisibilityScope.USER_ONLY,
            with_interests=bool(interests_state),
            history_ids=history_ids,
            working_context=working_context,
            people_history=people_history,
            inbox_attention=inbox_attention,
            can_quote=can_quote,
            history_route=history_route,
            history_visibility=target.visibility,
            language_scope_key=route_key or local_language_scope(target),
            summary_scope=summary_scope_for(
                target.channel,
                target.sender_id,
                target.trust,
                target.visibility,
                route_key,
            ),
            legacy_user_extractions=(
                target.channel == "terminal"
                and viewer_is_user
                and target.visibility is VisibilityScope.USER_ONLY
            ),
            emoji_history=(
                [
                    record
                    for record in self._recent_history(target, EMOJI_CONTEXT_LIMIT)
                    if record.id not in batch_ids
                ]
                if self.emoji_selector is not None and target.channel == "qq" and target.route
                else None
            ),
        )
        job = self._apply_hormones(job, (), now=now)
        owner_private = not contact and target.visibility is VisibilityScope.USER_ONLY
        if (
            messages
            and (owner_private or target.route is not None)
            and target.trust is not TrustLevel.UNVERIFIED
            and not self._config.roleplay_mode
            and self._judge is not None
            and self._judge.enabled
            and self._judge.state_context_enabled
        ):
            state = (
                describe_judge_body(reading, body.config, owner_private=owner_private)
                if body is not None and reading is not None
                else []
            )
            if job.effects is not None and (levels := job.effects.describe_levels()):
                state.append(f"情绪（0 至 1）：{levels}")
            if job.mood_reading is not None:
                state.append(f"心情（−1 至 1）：{job.mood_reading.valence:+.2f}")
            if state:
                job = replace(job, judge_context=build_judge_context(identity, state))
        return job

    def _apply_hormones(
        self, job: _ReplyJob, extra: Sequence[HormoneTrigger], *, now: datetime
    ) -> _ReplyJob:
        """把激素的影响放进这次回复：摘要和行为说明进上下文，低落时回复长度降一档（D69 第 3 条）。

        Args:
            job: 回复要用的材料；每次都从 base_strategy 重新算，重复调用不会叠加。
            extra: 刚算出、还没正式记下的触发。后台任务不写状态（D41），回复却要带上这次的情绪，
                所以先拿它们临时算，正式写入在收结果的那一拍。
            now: 本次计算激素影响使用的时钟时间。
        """
        hormones = self._hormones
        empathic = self._empathy_focus.active(trigger for trigger in extra if trigger.is_empathy)
        if not job.empathy_detail_for_all:
            visible_ids = set(job.history_ids)
            empathic = tuple(
                trigger
                if trigger.source_record_id in visible_ids
                else replace(trigger, person_id=None, reason="另一段对话里的事")
                for trigger in empathic
            )
        effective_triggers = (*empathic, *(trigger for trigger in extra if not trigger.is_empathy))
        effects = (
            hormones.effects(
                now,
                person_id=job.person_id,
                viewer_is_user=job.viewer_is_user,
                extra=effective_triggers,
            )
            if hormones is not None
            else None
        )
        strategy = job.base_strategy
        length_note = ""
        if effects is not None and effects.shorten_reply:
            current = strategy.length_limit or ReplyLength.LONG
            length = current.lowered()
            strategy = replace(strategy, length_limit=length)
            length_note = f"低落使本轮回复长度上限调整为：{length.describe()}。"

        def hormone_context(*, plan_first: bool) -> str:
            if effects is None:
                return ""
            return "\n".join(
                filter(None, (effects.context_text(plan_first=plan_first), length_note))
            )

        mood = job.read_mood(now, effective_triggers) if job.read_mood is not None else None
        full_context, replyer_context = job.build_contexts(
            hormone_context(plan_first=strategy.plan_first),
            hormone_context(plan_first=False),
            mood.summary if mood is not None else None,
        )
        return replace(
            job,
            strategy=strategy,
            full_context=full_context,
            replyer_context=replyer_context,
            effects=effects,
            mood_reading=mood,
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
        originals = job.messages
        if job.media_sources:
            messages = await self.inbox_media.enrich(
                job.messages, job.media_sources, job.strategy.model_preference
            )
            job = replace(job, messages=messages)
        attempt = await self._judge_emotion(job)
        if job.round_trace is not None:
            job.round_trace.judge = attempt
        if attempt is not None or job.media_sources:
            job = self._apply_hormones(
                job, attempt.triggers if attempt is not None else (), now=self._clock.now()
            )
        recall = _RoundRecall()
        if self._memory_config.heuristic_recall_enabled and self._can_recall():
            words = heuristic_keywords(originals)
            if words and (hits := await self._retrieve(job, words)):
                recall.automatic = words, hits
        try:
            plan, recalled = await self._plan(job, recall)
            if job.planner_run is not None and job.planner_run.restart_requested:
                return _ThoughtOutcome(declined=True)
            outcome = (
                await self._reply_with_review(job, plan, recall, recalled)
                if plan is not None and plan.should_reply
                else _ThoughtOutcome(plan=plan, declined=True)
            )
        except _SummaryRecallChangedError:
            if job.round_trace is not None:
                job.round_trace.actions[:] = [
                    action
                    if action.action != "reply" or action.status != "completed"
                    else PlannerAction("reply", "rejected", "记忆来源已变化，丢弃本轮建议")
                    for action in job.round_trace.actions
                ]
            _trace_action(
                job, PlannerAction("query_memory", "rejected", "记忆来源已变化，本轮不发言")
            )
            return _ThoughtOutcome(declined=True, judge=attempt, effects=job.effects)
        except CallRejectedError as rejection:
            return _ThoughtOutcome(
                notice=f"没能调用模型，这次没有回复：{rejection.reason}", judge=attempt
            )
        if outcome.response is not None:
            outcome = await self._finish_reply(job, outcome, outcome.response)
            if outcome.response is None:
                _trace_action(job, PlannerAction("replyer", "failed", "没有可发送的正文"))
            else:
                _trace_action(job, PlannerAction("replyer", "completed", "已生成正文，等待投递"))
        return replace(
            outcome,
            reference_plan=outcome.reference_plan or plan,
            judge=attempt,
            effects=job.effects,
        )

    async def _reply_with_review(
        self,
        job: _ReplyJob,
        plan: Plan,
        recall: _RoundRecall,
        recalled: _Recall | None,
    ) -> _ThoughtOutcome:
        """把有效的驳回理由交给规划器重审一次。"""
        outcome = await self._reply(job, plan, recall, recalled)
        if outcome.review_reason is None:
            return outcome
        if not outcome.review_reason:
            _trace_action(job, PlannerAction("replyer", "rejected", "驳回格式无效，不发言"))
            return _ThoughtOutcome(declined=True)
        _trace_action(job, PlannerAction("replyer", "rejected", "回复器要求重新审视"))
        _trace_action(job, PlannerAction("review", "requested", "重新判断是否需要回复"))
        try:
            revised, recalled = await self._plan(job, recall, review_reason=outcome.review_reason)
        except CallRejectedError:
            return _ThoughtOutcome(declined=True)
        if revised is None or not revised.should_reply:
            return _ThoughtOutcome(declined=True, reference_plan=revised)
        outcome = await self._reply(job, revised, recall, recalled)
        if outcome.review_reason is not None:
            _trace_action(job, PlannerAction("replyer", "rejected", "重新审视后仍被驳回，不发言"))
            outcome = _ThoughtOutcome(declined=True)
        return replace(outcome, reference_plan=revised)

    async def _finish_reply(
        self, job: _ReplyJob, outcome: _ThoughtOutcome, response: OrganResponse
    ) -> _ThoughtOutcome:
        """再审通过后才整理实际发言；快速模型只看到要分段的文字。"""
        draft = prepare_reply(response.suggestion)
        if not draft.sections:
            return replace(outcome, response=None, notice="模型返回了空回复，这次没有回复。")
        text = draft.text.strip()
        if self._config.split_reply_enabled:
            suggestion = (
                await self._suggest_reply_splits(job, draft) if self.has_fast_model else None
            )
            sections = draft.segments(
                suggestion,
                max_messages=self._config.max_reply_messages,
                min_chars=self._config.min_message_chars,
            )
        else:
            sections = (text,)
        if not sections:
            return replace(outcome, response=None, notice="模型返回了空回复，这次没有回复。")
        emoji = None
        if (
            self.emoji_selector is not None
            and job.emoji_history is not None
            and outcome.plan is not None
            and outcome.plan.emoji is not None
        ):
            emoji = await self.emoji_selector.select(
                outcome.plan.emoji,
                [*job.emoji_history, *job.messages][-EMOJI_CONTEXT_LIMIT:],
                job.user_name,
                job.strategy.model_preference,
                has_embedding=self.has_text_embedding,
            )
        return replace(
            outcome,
            response=replace(response, suggestion=text),
            reply_segments=sections,
            emoji=emoji,
        )

    async def _suggest_reply_splits(self, job: _ReplyJob, draft: ReplyDraft) -> str | None:
        try:
            response = await self._call(
                OrganRequest(
                    "fast_model",
                    draft.prompt(),
                    context={"task": "reply_segments"},
                    model_preference=job.strategy.model_preference,
                )
            )
        except (CallRejectedError, OrganCallError, ValueError) as failure:
            logger.warning("快速模型未能建议切点，按文字分段：%s", failure)
            return None
        return response.suggestion

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
        if hormones is None:
            return ()
        now = self._clock.now()
        direct = (
            make_triggers_from_judgment(
                judgment.toward_me,
                person_id=person_id,
                source_record_id=latest.id,
                source_visibility=latest.visibility,
                now=now,
                config=hormones.config,
                recent_triggers=hormones.triggers,
            )
            if person_id is not None
            else []
        )
        return (*direct, *self._empathy_from(judgment, job, hormones, now))

    def _empathy_from(
        self, judgment: Judgment, job: _ReplyJob, hormones: Hormones, now: datetime
    ) -> tuple[HormoneTrigger, ...]:
        empathy = self._empathy
        if empathy is None or not empathy.enabled or not judgment.others:
            return ()
        focus = self._empathy_focus.source_ids()
        candidates = self._empathy_focus.candidates(focus)
        sources = {record.id: record for record in job.messages if record.id in focus}
        used = {
            (trigger.source_record_id, trigger.empathy_for.casefold())
            for trigger in candidates
            if trigger.empathy_for is not None
        }
        empathic: list[HormoneTrigger] = []
        for item in judgment.others:
            source_id = item.source_record_id
            if source_id is None and len(job.messages) == 1:
                source_id = job.messages[0].id
            source = sources.get(source_id) if source_id is not None else None
            if source is None or (person_id := _person_id_of(source)) is None:
                continue
            key = (source.id, item.subject.casefold())
            if key in used or (
                item.subject != SENDER_SUBJECT and key[1] not in source.content.casefold()
            ):
                continue
            triggers = make_triggers_from_empathy(
                (item,),
                person_id=person_id,
                affinity_of=lambda subject, sender_id=person_id: self._affinity_of(
                    subject, sender_id
                ),
                source_record_id=source.id,
                source_visibility=source.visibility,
                now=now,
                config=hormones.config,
                empathy=empathy,
                recent_triggers=(*candidates, *empathic),
            )
            if triggers and kind_enabled(triggers[0].kind, hormones.config):
                empathic.extend(triggers)
                used.add(key)
        return tuple(empathic)

    def _affinity_of(self, subject: str, sender_id: int) -> float:
        """共情对象的好感（D72 第 2 条）。

        发消息的人取他的好感；消息里提到的人或动物没有档案，好感账关着时，都按陌生人默认值。
        """
        if self._affinity is None:
            return _DEFAULT_STRANGER_AFFINITY
        if subject == SENDER_SUBJECT:
            return self._affinity.current(sender_id)
        return self._affinity.config.stranger_affinity

    def planner_tool_options(
        self,
        *,
        interests: bool,
        emoji: bool,
        inbox: bool,
        special_contact: bool,
        stay_in_conversation: bool,
        quote_source: bool,
        platform_history: bool = False,
        platform_cleanup: bool = False,
    ) -> PlannerToolOptions:
        """把会话范围和已装配的能力合成对话规划的工具选项。"""
        return PlannerToolOptions(
            agenda=self._agenda is not None,
            interests=interests,
            recall=self._can_recall(),
            expressions=self._can_recall_language(),
            emoji=emoji and self.emoji_selector is not None,
            inbox=inbox,
            special_contact=special_contact,
            stay_in_conversation=stay_in_conversation,
            quote_source=quote_source,
            platform_history=(
                platform_history
                and self.platform_history is not None
                and self.platform_history.allows is not None
                and not self._config.roleplay_mode
            ),
            platform_cleanup=(
                platform_cleanup
                and platform_history
                and self.history_retention is not None
                and self.history_retention.available()
                and not self._config.roleplay_mode
                and self.platform_history is not None
                and self.platform_history.allows is not None
            ),
        )

    async def _plan(
        self,
        job: _ReplyJob,
        recall: _RoundRecall,
        *,
        review_reason: str = "",
    ) -> tuple[Plan | None, _Recall | None]:
        """调用有界的工具规划；失败不发言，网关的拒绝照常抛出。"""
        organ_id = self._config.plan_organ
        history, key = self.platform_history, job.history_route

        def still_authorized() -> bool:
            return (
                history is not None
                and key is not None
                and history.permits(key)
                and (job.planner_run is None or not job.planner_run.restart_requested)
            )

        can_read_history = (
            job.history_visibility
            in (
                VisibilityScope.USER_ONLY,
                VisibilityScope.GROUP,
                VisibilityScope.PUBLIC,
            )
            and still_authorized()
        )
        options = self.planner_tool_options(
            interests=job.with_interests,
            emoji=job.emoji_history is not None,
            inbox=job.inbox_attention,
            special_contact=job.person_id is not None and not job.viewer_is_user,
            stay_in_conversation=any(
                "inbox_message_id" in record.metadata for record in job.messages
            ),
            quote_source=job.can_quote,
            platform_history=can_read_history,
            platform_cleanup=job.round_trace is not None and not review_reason,
        )
        tools = planner_tools(options)
        jargon = await self._jargon_reference(job)
        heuristic_recall = ""
        pending_hits: Sequence[MemoryHit] = ()
        if recall.automatic is not None:
            words, hits = recall.automatic
            verified = await self._verified_memory(job, hits)
            recall.automatic = None
            if verified:
                heuristic_recall, pending_hits = format_recall_hits(
                    words, verified, self._timezone, heuristic=True
                )
                recall.automatic = words, pending_hits
        prompt = build_plan_prompt(
            job.history,
            job.messages,
            job.user_name,
            self._timezone,
            with_agenda=options.agenda,
            with_interests=options.interests,
            with_recall=options.recall,
            jargon=jargon,
            with_expressions=options.expressions,
            heuristic_recall=heuristic_recall,
            review_reason=review_reason,
            tools=tools,
        )
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

        async def retrieve(words: tuple[str, ...]) -> tuple[str, _Recall] | None:
            nonlocal pending_hits
            hits = await self._retrieve(job, words)
            if hits is None:
                return None
            content, pending_hits = format_recall_hits(
                words, await self._verified_memory(job, hits), self._timezone
            )
            return content, (words, pending_hits)

        async def call_planner(request: OrganRequest) -> OrganResponse:
            nonlocal pending_hits
            recall.seen.extend(hit for hit in pending_hits if hit.is_summary_fact)
            pending_hits = ()
            await self._require_current_recall(job, recall)
            response = await self._call(request)
            await self._require_current_recall(job, recall)
            return response

        async def read_history(query: HistoryQuery) -> str:
            assert history is not None and key is not None
            return await asyncio.to_thread(history.read, key, job.history_visibility, query)

        pending: CleanupPreview | None = None
        trace = job.round_trace

        def mark_history_used() -> None:
            if trace is not None:
                trace.used_platform_history = True

        async def preview_cleanup(cutoff: date) -> str:
            nonlocal pending
            assert self.history_retention is not None and key is not None
            pending = await asyncio.to_thread(
                self.history_retention.preview,
                key,
                job.history_visibility,
                cutoff,
            )
            if pending is None:
                return '{"status":"invalid_cutoff","detail":"截止日期不能晚于当地今天。"}'
            return json.dumps(pending.summary(), ensure_ascii=False)

        try:
            result = await dispatch_planner_tools(
                request,
                tools,
                call_planner,
                retrieve,
                actions=trace.actions if trace is not None else None,
                read_history=read_history if options.platform_history else None,
                preview_cleanup=preview_cleanup if options.platform_cleanup else None,
                still_authorized=still_authorized if options.platform_history else None,
                history_used=mark_history_used,
                summarize_reply=trace is None or not trace.used_platform_history,
            )
            if pending is not None and still_authorized() and trace is not None:
                trace.cleanup_preview = pending
            return result
        except (CallRejectedError, _SummaryRecallChangedError):
            raise
        except OrganCallError as failure:
            logger.warning("规划器调用失败：%s", failure)
            return None, None
        except Exception:
            logger.exception("调用规划器 %s 时出错", organ_id)
            return None, None
        finally:
            if run is not None:
                run.is_planning = False

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
                self._language.jargon_reference,
                texts,
                self._language_config.reference_chars,
                conversation_key=job.language_scope_key,
            )
        except sqlite3.Error:
            logger.exception("读取黑话参考失败")
            return ""

    async def _expression_reference(self, job: _ReplyJob, plan: Plan, recall: _RoundRecall) -> str:
        if self._language is None or not self._can_recall_language():
            return ""
        if self.expression_recall is not None:
            vector_reference = await self.expression_recall.reference(
                plan,
                job.language_scope_key,
                recheck=lambda: self._require_current_recall(job, recall),
            )
            if vector_reference is not None:
                return vector_reference
        topic = "\n".join(record.content for record in job.messages)
        intent = " ".join((plan.intent, *plan.expression_focus))
        try:
            return await asyncio.to_thread(
                self._language.expression_reference,
                topic,
                intent,
                plan.expression_avoid,
                speaker_key(job.messages[-1]),
                self._language_config.reference_chars,
                conversation_key=job.language_scope_key,
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
                include_user_extractions=job.legacy_user_extractions,
                summary_scope=job.summary_scope,
            )
            if self.has_text_embedding and len(hits) > 1:
                hits = await self._rerank_memory(words, hits)
        except Exception:
            logger.exception("记忆检索失败，这次检索不附记忆")
            return None
        if person_keys is not None or not self._memory_config.graph_recall_enabled:
            return hits[:limit]
        try:
            graph_hits = await asyncio.to_thread(self._memory.search_graph_recall, words, limit)
        except sqlite3.Error:
            logger.exception("关系图召回失败，仅保留文字检索")
            return hits[:limit]
        relation_keys = {hit.source.encode() for hit in graph_hits if hit.source is not None}
        hits = [
            hit for hit in hits if hit.source is None or hit.source.encode() not in relation_keys
        ]
        return [*graph_hits, *hits][:limit]

    async def _verified_memory(
        self, job: _ReplyJob, hits: Sequence[MemoryHit]
    ) -> Sequence[MemoryHit]:
        if not any(hit.is_summary_fact for hit in hits):
            return hits
        if self._memory is None:
            return [hit for hit in hits if not hit.is_summary_fact]
        return await asyncio.to_thread(self._memory.recheck_summary_hits, hits, job.summary_scope)

    async def _require_current_recall(self, job: _ReplyJob, recall: _RoundRecall) -> None:
        if len(await self._verified_memory(job, recall.seen)) != len(recall.seen):
            raise _SummaryRecallChangedError

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
        recall: _RoundRecall,
        recalled: _Recall | None,
    ) -> _ThoughtOutcome:
        """让回复器按明确的发言规划写成发言或通过内部标记驳回（D103）。"""
        strategy = job.strategy
        await self._require_current_recall(job, recall)
        length = (
            plan.length.capped_at(strategy.length_limit)
            if plan.length is not None
            else strategy.length_limit
        )
        sentinel = _REVIEW_SENTINEL_PREFIX + uuid4().hex
        recall_text = ""
        keywords: tuple[str, ...] = ()
        hits: Sequence[MemoryHit] = ()
        if recalled is not None:
            keywords, explicit = recalled
            recall_text, hits = format_recall_hits(keywords, explicit, self._timezone)
        elif recall.automatic is not None:
            keywords, candidates = recall.automatic
            recall_text, hits = format_recall_hits(
                keywords, candidates, self._timezone, heuristic=True
            )
        request = replace(
            build_reply_request(
                self._config.reply_organ,
                job.history,
                job.messages,
                job.user_name,
                self._timezone,
                system_context=job.replyer_context,
                working_context=job.working_context,
                intent=plan.intent,
                plan_reason=plan.reason,
                reply_reference=plan.reply_reference,
                addressed_to_me=plan.addressed_to_me,
                review_sentinel=sentinel,
                with_splits=self._config.split_reply_enabled,
                length=length,
                recall=recall_text,
                jargon=await self._jargon_reference(job),
                expressions=await self._expression_reference(job, plan, recall),
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
            context={"task": "reply", "in_reply_to": [record.id for record in job.messages]},
            purpose=CallPurpose.USER_REPLY,
            model_preference=job.strategy.model_preference,
        )
        await self._require_current_recall(job, recall)
        try:
            response = await self._call(request)
        except CallRejectedError as rejection:
            _trace_action(job, PlannerAction("replyer", "rejected", "安全网关拒绝生成回复"))
            return _ThoughtOutcome(notice=f"没能调用模型，这次没有回复：{rejection.reason}")
        except OrganCallError as failure:
            _trace_action(job, PlannerAction("replyer", "failed", "回复器调用失败"))
            return _ThoughtOutcome(notice=f"模型调用失败，这次没有回复。{failure}")
        except Exception as error:
            _trace_action(job, PlannerAction("replyer", "failed", "回复器调用失败"))
            # 为什么捕获所有异常：后台任务里的异常没人接住，用户只会看到它一直不说话。
            logger.exception("调用器官 %s 时出错", request.organ_id)
            return _ThoughtOutcome(
                notice=f"调用模型时出错（{type(error).__name__}），这次没有回复，详情见日志。"
            )
        await self._require_current_recall(job, recall)
        if self.expression_recall is not None:
            self.expression_recall.backfill(job.language_scope_key)
        text = response.suggestion.strip()
        if not text:
            _trace_action(job, PlannerAction("replyer", "failed", "没有可发送的正文"))
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
            recall_feedback_refs=(
                tuple(
                    f"source:{hit.source.encode()}"
                    if hit.source is not None
                    else f"timeline:{hit.record_id}"
                    for hit in hits
                    if hit.source is not None or hit.record_id is not None
                )
                if self._memory_config.recall_feedback_enabled
                else ()
            ),
        )
