"""心跳 heartbeat：常驻主循环（02 号文档第 3 节，03 号文档“心跳”一节，D41）。

每一拍按顺序做：收感知（写进时间线）→ 对结果（想法有结果了就说出来）→ 量身体 →
定焦点、想一想（有没回复的消息就在后台调用器官）→ 广播认知状态。
想一想的做法由精力档位定（D59）：正常档先调规划器、再调回复器，低档位合并成一次调用。
急停中（D54）照常收感知、记时间线，但不调用器官、不回复，只发系统通知。

一拍里只有很短的同步操作。慢的器官调用放进后台任务，心跳照常跳；同一时间只想一件事
（D34：前额叶是串行的）。所有状态写入都在拍里完成，后台任务只负责等器官的建议，
有了结果就叫醒心跳，由下一拍处理。
"""

import asyncio
import contextlib
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, replace
from datetime import datetime, timedelta, tzinfo
from enum import Enum, auto
from typing import Any, cast

from aureus.actions.search import SearchProvider
from aureus.actions.web import WebReader
from aureus.agenda import AgendaStore, describe_agenda
from aureus.agenda.findings import FindingStore
from aureus.agenda.interests import InterestStore
from aureus.attention import Inbox, Priority, SummaryOutcome, WorkingMemory, summarize
from aureus.attention.working_memory import ORGAN as WORKING_MEMORY_ORGAN
from aureus.attention.working_memory import TASK as WORKING_MEMORY_TASK
from aureus.body import Body, BodyReading, BodySampleStore, EnergyTier, sample_from_reading
from aureus.common import (
    BudgetCalendar,
    Clock,
    DiaryConfig,
    EmpathyConfig,
    ExplorationConfig,
    HeartbeatConfig,
    IndexingConfig,
    InitiativeConfig,
    JudgeConfig,
    LanguageConfig,
    MemoryConfig,
    PeopleConfig,
    PersonaGrowthConfig,
)
from aureus.common.config import CortisolConfig
from aureus.common.web_identity import WEB_CHANNEL_NAME, WEB_USER_ID
from aureus.core import CognitiveObserver, CognitiveState
from aureus.feedback import FeedbackStore, Outcome
from aureus.heartbeat import exploration_runner, interest_changes, language_runner
from aureus.heartbeat import persona_growth_runner as growth
from aureus.heartbeat.agenda_hooks import AgendaHooks
from aureus.heartbeat.chat_plugins import AssetNames, GroupReader
from aureus.heartbeat.context import build_initiative_plan_prompt, build_initiative_reply_prompt
from aureus.heartbeat.conversation import (
    ConversationFlow,
    OrganCaller,
    _Heard,
    _InitiativeJob,
    _JudgeAttempt,
    _organ_request,
    _PendingJudgment,
    _person_id_of,
    _PlannerRun,
    _reply_job,
    _ReplyJob,
    _RoundTrace,
    _Thought,
    _ThoughtOutcome,
    with_mention_pull,
)
from aureus.heartbeat.cortisol import CortisolTiming
from aureus.heartbeat.debounce import MessageDebouncer
from aureus.heartbeat.diary_runner import DiaryOutcome, DiaryTracker, write_diary
from aureus.heartbeat.downtime import Downtime, assess_downtime
from aureus.heartbeat.emoji_followups import StickerFollowups, StickerRound
from aureus.heartbeat.emoji_maintenance import EmojiMaintenance
from aureus.heartbeat.emoji_selection import EmojiSelector
from aureus.heartbeat.empathy import EmpathyFocus
from aureus.heartbeat.events import (
    EVENT_COGNITIVE_STATE,
    EVENT_EMERGENCY_RESUME,
    EVENT_EMERGENCY_STOP,
    EVENT_HORMONE_TRIGGERED,
    EVENT_IDENTITY_CHANGED,
    EVENT_JUDGMENT,
    EVENT_MESSAGE,
    EVENT_ORGAN_CALL_ABANDONED,
    EVENT_PLANNING_INTERRUPTED,
    EVENT_REPLY_DECLINED,
    EVENT_REPLY_DELIVERED,
    EVENT_REPLY_DELIVERY_FAILED,
    EVENT_REPLY_REFERENCE,
    EVENT_SELF_CHECK,
    EVENT_SHUT_DOWN,
    EVENT_WOKE_UP,
)
from aureus.heartbeat.expression_recall import ExpressionRecall
from aureus.heartbeat.history_retention import HistoryRetention
from aureus.heartbeat.inbox_attention import (
    SELECTION_TASK,
    InboxAttention,
    InboxResidence,
    InboxSelection,
    select_conversations,
)
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_media import AttachmentReader
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.heartbeat.initiative import InitiativeDecision, decide
from aureus.heartbeat.initiative_runner import InitiativeTracker, read_last_conversation
from aureus.heartbeat.judging import Judgment, TowardMeKind
from aureus.heartbeat.live import (
    RESOURCE_EMOJI,
    RESOURCE_EXPRESSION,
    RESOURCE_IMAGES,
    RESOURCE_LANGUAGE,
    RESOURCE_MEMORY,
    TOPIC_INBOX,
    TOPIC_RESOURCES,
    TOPIC_ROUND,
    LiveEvent,
    LiveEventHub,
)
from aureus.heartbeat.memory_consolidation import (
    ORGAN as MEMORY_CONSOLIDATION_ORGAN,
)
from aureus.heartbeat.memory_consolidation import ConsolidationOutcome, MemoryConsolidator
from aureus.heartbeat.memory_summary_source import PlatformSummarySource
from aureus.heartbeat.pending_learning_buffer import PendingLearningBuffer
from aureus.heartbeat.perception import (
    MessageKind,
    MessageRoute,
    MessageSink,
    OutgoingMessage,
    Perception,
    QQChatAction,
    TrustLevel,
)
from aureus.heartbeat.planner_tools import planner_tools
from aureus.heartbeat.planning import InitiativePlan, Plan, parse_initiative_plan
from aureus.heartbeat.platform_history import PlatformHistory
from aureus.heartbeat.recall import MemoryTracker
from aureus.heartbeat.reply_outbox import AtReplyPart, EmojiReplyPart, PartReceipt, ReplyOutbox
from aureus.heartbeat.reply_records import INTERNAL_REPLY_RECORD, delivery_callbacks
from aureus.heartbeat.reply_strategy import ReplyStrategy, choose_strategy
from aureus.heartbeat.self_check import describe_self_check
from aureus.hormones import (
    HormoneEffects,
    HormoneKind,
    Hormones,
    HormoneTrigger,
    RetaliationMode,
    kind_enabled,
)
from aureus.identity import Identity, IdentityHistory, IdentityLayer
from aureus.memory import (
    DiaryBook,
    EmojiStore,
    ExpressionVectorIndex,
    LanguageStore,
    MemoryIndex,
)
from aureus.mood import FeedbackOutcome, Mood, MoodReading
from aureus.organs import CallPurpose, OrganCallError, OrganRequest, OrganResponse, ToolDefinition
from aureus.organs.topology import resolve_task_organ
from aureus.people import (
    AffinityBook,
    AffinityEvent,
    AffinityReason,
    PeopleDirectory,
    Person,
    describe_affinity,
)
from aureus.safety import (
    ACTION_CALL_ORGAN,
    ACTION_REPLY_TO_USER,
    CallRejectedError,
    EmergencyStop,
    HaltStatus,
    PermissionChecker,
)
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

logger = logging.getLogger(__name__)

# 认知状态里的“当前任务”（CognitiveObserver 的 task_name）。
TASK_REPLYING = "回复用户"
TASK_IDLE = "待机"

# 系统通知显示的发送者：通知不是它说的话（D35）。
NOTICE_SENDER = "系统"

# 写进关闭记录的原因。
SHUTDOWN_REQUESTED = "按要求关闭"
SHUTDOWN_INTERRUPTED = "被中断（比如按了 Ctrl+C）"
SHUTDOWN_AFTER_ERROR = "心跳出错后关闭"

# 压力回落到偏高线的这个比例以下，才算「缓过来了」，下次偏高时再发自检通知。
# 不在刚低于偏高线时就重置，免得压力在线上下抖动时反复发通知。
_STRESS_REARM_FRACTION = 0.5

# 取消进行中的器官调用后，最多再等它这么多秒。正常情况下取消会立刻生效；
# 万一器官代码吞掉了取消，也不能让关闭流程永远卡住。
_CANCEL_WAIT_SECONDS = 5.0
# 后台索引构建（D125）：连续这么多轮没有任何进展就暂停到下一轮，避免对着坏数据空转。
_INDEX_BUILD_MAX_STALLED_ROUNDS = 3
# 网关因预算或精力拒绝后至少等这么久再试；心跳本身每拍也会重新检查。
_INDEXING_REJECTED_PAUSE_SECONDS = 300.0
# 构建器按这三种索引轮转，与器官面板的统计种类同名。
_INDEX_EXPRESSION = "expression"
_INDEX_EMOJI = "emoji_cache"
_INDEX_IMAGES = "images"
# 索引种类对应网页上的资源名；每建成一批就让对应页面的缓存失效（3.43）。
_RESOURCE_BY_INDEX = {
    _INDEX_EXPRESSION: RESOURCE_EXPRESSION,
    _INDEX_EMOJI: RESOURCE_EMOJI,
    _INDEX_IMAGES: RESOURCE_IMAGES,
}
# 重启时最多读多少条激素触发记录；超过半衰期 8 倍的会被 Hormones 丢掉，这里只防读得太多。
_HORMONE_RESTORE_LIMIT = 1000
# /state 里激素的显示名（09 号文档术语表）。
# 好感账关着时，共情按陌生人的默认好感算（D72 第 2 条）。
_DEFAULT_STRANGER_AFFINITY = PeopleConfig().stranger_affinity
# 判断器官报的事怎么换成好感变化的原因（D71 第 1 条）。
_AFFINITY_REASONS = {
    TowardMeKind.PRAISE: AffinityReason.PRAISE,
    TowardMeKind.OFFENSE: AffinityReason.OFFENSE,
    TowardMeKind.REJECTION: AffinityReason.REJECTION,
}
_HORMONE_NAMES = {"anger": "怒气", "joy": "愉悦", "hurt": "低落", "surprise": "惊讶"}


class _Phase(Enum):
    NOT_STARTED = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()


class Heartbeat:
    """常驻主循环。用法：`await heartbeat.run()`，要停时调用 `request_stop()`。

    wake_up()、beat()、shut_down() 也单独公开，测试可以一拍一拍地驱动。
    """

    def __init__(
        self,
        *,
        clock: Clock,
        timeline: TimelineDatabase,
        organs: OrganCaller,
        identity: Identity,
        config: HeartbeatConfig,
        indexing: IndexingConfig | None = None,
        permissions: PermissionChecker,
        emergency_stop: EmergencyStop,
        timezone: tzinfo | None = None,
        body: Body | None = None,
        people: PeopleDirectory | None = None,
        people_config: PeopleConfig | None = None,
        identity_history: IdentityHistory | None = None,
        samples: BodySampleStore | None = None,
        feedback: FeedbackStore | None = None,
        judge: JudgeConfig | None = None,
        hormones: Hormones | None = None,
        affinity: AffinityBook | None = None,
        empathy: EmpathyConfig | None = None,
        mood: Mood | None = None,
        agenda: AgendaStore | None = None,
        initiative: InitiativeConfig | None = None,
        cortisol: CortisolConfig | None = None,
        calendar: BudgetCalendar | None = None,
        interests: InterestStore | None = None,
        findings: FindingStore | None = None,
        exploration: ExplorationConfig | None = None,
        search: SearchProvider | None = None,
        reader: WebReader | None = None,
        has_exploration_organ: bool = True,
        memory: MemoryIndex | None = None,
        emoji_store: EmojiStore | None = None,
        memory_config: MemoryConfig | None = None,
        language: LanguageStore | None = None,
        language_config: LanguageConfig | None = None,
        expression_index: ExpressionVectorIndex | None = None,
        diary: DiaryBook | None = None,
        diary_config: DiaryConfig | None = None,
        has_diary_organ: bool = True,
        persona_growth: PersonaGrowthConfig | None = None,
        has_persona_organ: bool = True,
        inbox_store: InboxStore | None = None,
        working_memory: WorkingMemory | None = None,
    ) -> None:
        """创建心跳。时间线要由调用方先打开。

        Args:
            clock: 时钟；所有时间都从它取。
            timeline: 时间线。
            organs: 调用器官用的对象，核心组装时传安全网关。
            identity: 身份事实和四层人设。
            config: 心跳配置。
            indexing: 自动建索引的批大小、批间隔和总开关（D125）。
            permissions: 权限检查器，要和安全网关用同一个。
            emergency_stop: 急停开关，每拍读一次状态；要和权限检查器里的是同一个。
            timezone: 给人看的时间用哪个时区；不给就用这台电脑设置的时区。
                生物状态没有自己设时区时，日周期也按这个时区算（D42 第 3 条）。
            body: 身体，提供精力读数；不给时精力记为「不限」。
            people: 人物档案，收感知时按（通道, 账号）认人；
                不给就不认人，只按通道给的可信度分用户和别人。
            people_config: 人际配置，控制回复是否附带历史资料。
            identity_history: 身份的版本历史，要已经和 identity.toml 对过；醒来时据此记下用户改了
                哪几层设定（D62）。不给就不记。
            samples: 身体信号样本存储，要已经打开；心跳按 [body] sample_interval_seconds
                定时记一行读数，观察台画曲线靠它（D64）。不给就不记样本。
            feedback: 反馈存储，要已经打开；认知状态里的信心读它（D67）。不给就显示没有数据。
            judge: 判断器官的配置；不给或 enabled 为 False 就不调用判断（D68）。
            hormones: 激素的内存状态，醒来时从时间线还原；不给就没有激素（D69）。
            cortisol: 以本地时相和承诺失败推迟可选主动联系的配置（3.8）。
            affinity: 好感账，要已经打开；判断过的对话据此记好感变化，好感写进上下文（D71）。
                不给就没有好感。
            empathy: 同理心配置；不给或 enabled 为 False 就不共情（D72）。共情需要激素。
            mood: 心情；激素触发和反馈结果累积成心情，写进上下文，低时谨慎（D73）。
                醒来时从时间线还原；不给就没有心情。心情的激素来源需要激素。
            agenda: 日程，要已经打开；规划器答应的事记进去，到期提醒、兑现记进反馈（D74）。
                不给就没有日程。
            initiative: 主动联系的开关和退避参数；不给时关闭（D76）。
            calendar: 每日次数的分界，与预算共用日历；不给时按 timezone 创建。
            interests: 兴趣存储，开关开启时允许用户私聊提出修改（D79）。
            findings: 私密收获存储（D79）。
            exploration: 探索配置（D79）；不给时关闭。
            search: 搜索服务；不给时不探索。
            reader: 只读网页工具；不给时按探索配置构造。
            has_exploration_organ: 总结器官是否已注册；运行时传入真实登记结果。
            memory: 已打开的记忆索引；不给就不召回（D80）。
            emoji_store: 已打开的 QQ 表情资源库，供收集与回复选择。
            memory_config: 记忆索引和召回的开关及上限（D80）。
            language: 统一的习得语言库（D89）。
            language_config: 语言召回、增量学习和参考字数配置。
            expression_index: 启用表达向量时的派生索引。
            diary: 日记文件写入接口；不给就不写日记（D80）。
            diary_config: 日记器官、写作时刻和长度限制（D80）。
            has_diary_organ: 总结器官是否已注册；缺少时只记一次日志（D68、D80）。
            persona_growth: 默认关闭的人设成长配置（D81）。
            has_persona_organ: 人设成长的总结器官是否已注册。
            inbox_store: 平台收件箱，未配置 QQ 时不启用。
            working_memory: 已感知经历的原始焦点与滚动摘要。
        """
        self._clock = clock
        self._timeline = timeline
        self._working_memory = working_memory
        self._organs = organs
        self._identity = identity
        self._config = config
        self._indexing = indexing or IndexingConfig()
        self._indexing_task: asyncio.Task[None] | None = None
        self._indexing_retry_at: datetime | None = None
        self._text_spaces: tuple[tuple[int, str], ...] = ()
        self._inbox_image_index: InboxImageIndex | None = None
        self._permissions = permissions
        self._emergency_stop = emergency_stop
        self._halt_status = HaltStatus(halted=False)  # 最近一拍看到的急停状态
        self._timezone = timezone
        self._body = body
        self._people = people
        self._platform_inbox = (
            InboxAttention(
                inbox_store,
                timeline,
                people,
                ((account.platform, account.id) for account in identity.user.accounts),
                enabled=config.inbox_attention_enabled and not config.roleplay_mode,
                read_limit=config.inbox_read_limit,
                clock=clock,
                residence_seconds=config.inbox_residence_seconds,
                mention_pull_enabled=config.mention_pull_enabled,
            )
            if inbox_store is not None
            else None
        )
        self._identity_history = identity_history
        self._samples = samples
        self._last_sample_at: datetime | None = None  # 上一次记样本的时刻
        self._identity_changes: list[IdentityLayer] = []  # 这次醒来前用户改了哪几层
        self._live_identity_changes: list[IdentityLayer] = []  # 运行中刚改的，只说一次
        self._reading: BodyReading | None = None  # 最近一次读到的身体信号
        self._stress_alarmed = False  # 这一轮压力偏高的自检通知发过了
        self._stress_lowering = False  # 压力偏高，回复做法降一档中（D70），回落有滞后
        self._last_user_contact: Perception | None = None  # 自检通知发往的地方
        self._last_conversation_at: datetime | None = None  # 用户说了、它实际回了才更新（D76）
        self._observer = CognitiveObserver(clock, feedback)
        self._judge = judge
        self._hormones = hormones
        self._affinity = affinity
        self._empathy_focus = EmpathyFocus(
            timeline,
            working_memory,
            (
                hormones.config
                if hormones is not None and empathy is not None and empathy.enabled
                else None
            ),
        )
        self._mood = mood
        self._agenda = agenda
        self._feedback = feedback
        self._agenda_hooks = AgendaHooks(agenda, timeline, clock, feedback, timezone)
        self._language = language
        self._language_config = language_config or LanguageConfig()
        self._language_learners: dict[
            tuple[str, language_runner.LanguageKind], language_runner.TrimmedLearner
        ] = {}
        self._language_last_status = "等待新样本"
        # 模型热重载提供当前路线；清空备用器官的模型即可关闭其路由。
        self._available_organs: frozenset[str] = frozenset()
        self._emoji_maintenance = (
            EmojiMaintenance(emoji_store)
            if config.qq_emoji_maintenance_enabled and emoji_store is not None
            else None
        )
        self._conversation = ConversationFlow(
            clock=clock,
            timeline=timeline,
            config=config,
            call=self._call_organ,
            timezone=timezone,
            judge=judge,
            hormones=hormones,
            affinity=affinity,
            empathy=empathy,
            empathy_focus=self._empathy_focus,
            agenda=agenda,
            memory=memory,
            emoji_store=emoji_store,
            memory_config=memory_config,
            language=language,
            language_config=self._language_config,
            expression_index=expression_index,
            people=people,
            people_config=people_config,
            working_memory=working_memory,
            platform_history=(
                PlatformHistory(inbox_store, timeline.platform_history) if inbox_store else None
            ),
            history_retention=(
                HistoryRetention(inbox_store, timeline.platform_history, clock, timezone)
                if inbox_store is not None
                else None
            ),
            inbox_store=inbox_store,
        )
        if self._conversation.expression_recall is not None:
            self._conversation.expression_recall.set_scope_provider(self._expression_scopes)
        self._memory = MemoryTracker(memory, memory_config or MemoryConfig(), people, findings)
        self._memory_consolidator = (
            MemoryConsolidator(memory, self._memory.config)
            if memory is not None
            and self._memory.config.consolidation_enabled
            and not config.roleplay_mode
            else None
        )
        if self._memory_consolidator is not None and memory is not None:
            source = PlatformSummarySource(inbox_store, memory, timeline)
            source.web_enabled = lambda: WEB_CHANNEL_NAME in self._sinks
            self._memory_consolidator.source = source
            memory.summary_evidence = source.check_evidence
            memory.summary_scope_allowed = source.allows_scope
        self._diary: DiaryTracker | None = None
        diary_config = diary_config or DiaryConfig()
        if diary is not None:
            self._diary = DiaryTracker(
                timeline,
                calendar or BudgetCalendar(timezone),
                diary,
                diary_config,
                people,
                findings,
                has_diary_organ,
            )
        self._persona_growth: growth.PersonaGrowthTracker | None = None
        if identity_history is not None:
            self._persona_growth = growth.PersonaGrowthTracker(
                timeline,
                identity_history,
                persona_growth or PersonaGrowthConfig(),
                calendar or BudgetCalendar(timezone),
                has_persona_organ,
                clock,
                diary_config=diary_config
                if diary is not None
                else diary_config.model_copy(update={"enabled": False}),
            )
        initiative_config = initiative or InitiativeConfig()
        if config.roleplay_mode:
            initiative_config = initiative_config.model_copy(update={"enabled": False})
        calendar = calendar or BudgetCalendar(timezone)
        self._initiative = InitiativeTracker(initiative_config, timeline, feedback, calendar)
        self._cortisol = (
            CortisolTiming(cortisol, calendar.timezone, feedback)
            if cortisol is not None and cortisol.enabled and initiative_config.enabled
            else None
        )
        self._sinks: dict[str, MessageSink] = {}
        self._receipt_channels: set[str] = set()
        self._quote_routes: dict[str, Callable[[MessageRoute], bool]] = {}
        self._sticker_followups = StickerFollowups(clock)
        self._reply_outbox = ReplyOutbox(
            clock,
            config,
            self._send_reply_part,
            self._record_discarded_reply,
            lambda: self._wake.set(),
        )
        self._interests = None if config.roleplay_mode else interests
        self._findings = findings
        exploration = exploration or ExplorationConfig()
        self._exploration = exploration_runner.ExplorationTracker(
            timeline,
            calendar,
            self._interests,
            findings,
            exploration,
            search,
            reader or WebReader(exploration),
            has_exploration_organ,
        )
        self._pending: deque[tuple[Perception, InboxMessage | None]] = deque()
        self._unanswered: Inbox[_Heard] = Inbox()  # 记进时间线了、还没开始回复；用户先回（1.8）
        self._thought: _Thought | None = None
        # 判断与回复分开持有：回复交付后仍在跑、等后续拍收取（D110）。
        self._pending_judgments: list[_PendingJudgment] = []
        self._planner_restarts: dict[ConversationKey, int] = {}
        self._inbox_followups: set[ConversationKey] = set()
        self._debouncers: dict[ConversationKey, MessageDebouncer] = {}
        self._wake = asyncio.Event()
        self._phase = _Phase.NOT_STARTED
        self._stop_requested = False
        self._maintenance_paused = False
        self._maintenance_task: asyncio.Task[Any] | None = None
        self._downtime: Downtime | None = None
        self._cognitive_state: CognitiveState | None = None
        self._last_broadcast: str | None = None
        # 网页实时事件（3.43）：第一拍读到急停状态之前先不推送，免得停机中漏出事件。
        self.live_events = LiveEventHub()
        self.live_events.pause()
        self._last_round_state: tuple[bool, bool, ConversationKey | None] | None = None

    # ---------- 给通道用的接口 ----------

    @property
    def emoji_selector(self) -> EmojiSelector | None:
        """返回当前负责表情选择的实例，读取的是同一份内存缓存。"""
        return self._conversation.emoji_selector

    def update_available_organs(
        self,
        available: frozenset[str],
        *,
        expression_space: str | None = None,
        text_spaces: tuple[tuple[int, str], ...] = (),
    ) -> None:
        """模型热重载后同步可选总结任务的可用性，不改当天执行记录（D86）。

        嵌入路线变化时先停下后台构建；下一拍按新空间重新开始（D125）。
        """
        self._cancel_indexing()
        self._indexing_retry_at = None
        self._text_spaces = text_spaces
        self._available_organs = available
        if (
            self._thought is not None
            and self._thought.task.get_name() == language_runner.TASK_NAME
            and not self._has_language_organ()
        ):
            self._thought.task.cancel()
        self._conversation.has_text_embedding = "text_embedding" in available
        if self._conversation.expression_recall is not None:
            self._conversation.expression_recall.space = expression_space
        self._conversation.has_fast_model = "fast_model" in available
        self._conversation.inbox_media.available = available
        for task, tracker in (
            ("diary", self._diary),
            ("persona_review", self._persona_growth),
            ("exploration_summary", self._exploration),
        ):
            if tracker is not None:
                tracker.has_organ = (
                    resolve_task_organ(tracker.config.organ, task, available) in available
                )
        if self._memory_consolidator is not None:
            self._memory_consolidator.has_organ = (
                resolve_task_organ(MEMORY_CONSOLIDATION_ORGAN, "memory_consolidation", available)
                in available
            )

    def _inbox_attention_enabled(self) -> bool:
        return (
            self._platform_inbox is not None
            and self._config.inbox_attention_enabled
            and not self._config.roleplay_mode
        )

    def planner_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """返回当前运行中任一适用会话可声明的规划工具，不读取会话或调用模型。"""
        if self._config.plan_organ not in self._available_organs:
            return ()
        options = self._conversation.planner_tool_options(
            interests=interest_changes.interests_enabled(self._interests),
            emoji=self._platform_inbox is not None,
            inbox=self._inbox_attention_enabled(),
            special_contact=self._people is not None,
            stay_in_conversation=self._platform_inbox is not None,
            quote_source=bool(self._quote_routes),
            platform_history=True,
            platform_cleanup=True,
        )
        return tuple(tool.definition() for tool in planner_tools(options))

    @property
    def history_retention(self) -> HistoryRetention | None:
        """网页只读待确认范围，并在额外认证后执行确定性清理。"""
        return self._conversation.history_retention

    async def _call_organ(self, request: OrganRequest) -> OrganResponse:
        """在安全网关检查和记账之前选择实际承担该任务的器官。"""
        organ = resolve_task_organ(
            request.organ_id, request.context.get("task"), self._available_organs
        )
        if organ != request.organ_id:
            request = replace(request, organ_id=organ)
        return await self._organs.call(request)

    @property
    def expression_recall(self) -> ExpressionRecall | None:
        """网页维护与心跳共用同一个表达索引补建器。"""
        return self._conversation.expression_recall

    def attach_channel(
        self,
        name: str,
        sink: MessageSink,
        *,
        can_quote: Callable[[MessageRoute], bool] | None = None,
        confirms_delivery: bool = False,
    ) -> None:
        """接上消息通道，并可提供按目的路由检查引用能力的接口。

        confirms_delivery 表示通道接手后会报告成功或失败，不能把接手当作发完。

        Raises:
            ValueError: 同名的通道已经接上了。
        """
        if name in self._sinks:
            raise ValueError(f"通道 {name} 已经接上了")
        self._sinks[name] = sink
        if confirms_delivery:
            self._receipt_channels.add(name)
        if can_quote is not None:
            self._quote_routes[name] = can_quote

    def _can_quote_reply(self, target: Perception) -> bool:
        can_quote = self._quote_routes.get(target.channel)
        return target.route is not None and can_quote is not None and can_quote(target.route)

    def submit(self, perception: Perception) -> bool:
        """通道交来一条感知。心跳会立即跳一拍来处理它。

        Returns:
            收下了返回 True；正在关闭或已经关闭时不再收，返回 False。

        Raises:
            ValueError: 感知来自没有接上的通道（回复没处发，是程序写错了）。
        """
        if perception.channel not in self._sinks:
            raise ValueError(f"通道 {perception.channel} 没有接到心跳上")
        if self._maintenance_paused or self._phase in (_Phase.STOPPING, _Phase.STOPPED):
            logger.info("心跳正在关闭，没有收下来自 %s 的消息", perception.channel)
            return False
        self._pending.append((perception, None))
        self._wake.set()
        return True

    def set_inbox_route_filter(self, allows: Callable[[ConversationKey], bool]) -> None:
        """让已保存的未读也继续遵守当前通道允许的会话范围。"""
        if chat := self._conversation.chat_plugins:
            chat.allows = allows
        if self._platform_inbox is not None:
            self._platform_inbox.allows = allows
            if self._platform_inbox.residence is not None:
                self._wake.set()
        if self._language is not None:
            self._language.set_learning_source_filter(
                lambda scope: language_runner.allows_learning_scope(scope, allows)
            )
        if self._conversation.platform_history is not None:
            self._conversation.platform_history.allows = allows
        if (consolidator := self._memory_consolidator) is not None and consolidator.source:
            consolidator.source.allows = allows

    def set_chat_group_reader(
        self, reader: GroupReader, *, is_owner: Callable[[str], bool]
    ) -> None:
        """绑定通道的当前群名单与热更新后的真实主人身份。"""
        if chat := self._conversation.chat_plugins:
            chat.groups, chat.is_owner = reader, is_owner

    def set_chat_asset_names(self, names: AssetNames) -> None:
        """规划器只能查看当前会话的素材名，不获取路径或文件字节。"""
        if chat := self._conversation.chat_plugins:
            chat.assets = names

    def enable_qq_actions(self, enabled: bool) -> None:
        """只有已装配、允许动作的通道才向规划器开放原生动作。"""
        self._conversation.qq_actions_enabled = enabled

    def on_social_notice(self, notice: Perception) -> None:
        """已核定的戳/贴表情可以不理；跟撤只对同会话有直接来源的己方回执。"""
        inbox = self._platform_inbox
        if (
            inbox is None
            or not notice.components
            or not notice.route
            or not self.allows_emoji_delivery()
        ):
            return
        key = ConversationKey.from_perception(notice)
        if not inbox.allows(key):
            return
        component = notice.components[0]
        if component.kind != "recall":
            if component.kind in {"poke", "reaction"}:
                self.submit(notice)
            return
        for row in inbox.store.delivered_replies(key, component.target_message_id or ""):
            if row.perception.route is None:
                continue
            action = QQChatAction("recall", row.perception.route.message_id)
            record = self._timeline.append(
                EVENT_MESSAGE,
                "",
                ActorType.ME,
                visibility=row.perception.visibility,
                metadata={
                    "channel": key.platform,
                    "account_id": key.account_id,
                    "recipient_id": key.recipient_id,
                    "message_type": key.kind,
                    "temporary_group_id": key.temporary_group_id,
                    INTERNAL_REPLY_RECORD: True,
                    "nontext_reply": True,
                    "qq_action": "follow_recall",
                },
            )
            self._deliver_reply(
                notice,
                record,
                (),
                qq_action=action,
                can_deliver=lambda: self.allows_emoji_delivery() and inbox.allows(key),
            )

    def set_inbox_attachment_reader(self, reader: AttachmentReader) -> None:
        """接上通道提供的后台收件箱附件读取器。"""
        self._conversation.inbox_media.read = reader

    def set_inbox_image_indexer(self, indexer: InboxImageIndex) -> None:
        """把已经取回的图片交给同一收件箱的图像索引，并保留后台回填入口。"""
        self._inbox_image_index = indexer
        self._conversation.inbox_media.index_image = indexer.index

    def on_inbox_message(self, message: InboxMessage) -> None:
        """来信已保存，重要来信立即叫醒心跳；提及只唤醒，不抢占（D94、D137）。

        这个回调由 OneBot 读取任务直接调用；查库或认人失败只记日志，不能让它把读取任务带崩。
        """
        inbox = self._platform_inbox
        if self._maintenance_paused or inbox is None:
            return
        try:
            allowed = inbox.allows(message.conversation)
            if allowed:
                # 只带收件箱序号：网页据此让平台页失效并重新拉取，不推正文。
                self.live_events.publish(LiveEvent(TOPIC_INBOX, {"id": message.id}))
            if allowed and message.direction == "incoming":
                self._sticker_followups.cancel(message.conversation)
                if chat := self._conversation.chat_plugins:
                    chat.new_message(message)
                # 命令、通知和机器人自己不会走到这里；它们由通道直接归档（D142）。
                self._observe_debounce(message)
            if allowed and inbox.is_priority(message.perception):
                if inbox.preempts(message.perception):
                    self._preempt_inbox(inbox, message.conversation)
                self._wake.set()
            if message.conversation == self._inbox_followup_key():
                self._inbox_followups.add(message.conversation)
                self._wake.set()
        except (sqlite3.Error, RuntimeError):
            logger.exception("处理收件箱来信时读库失败，消息已保存，下一拍照常处理")

    def apply_identity_edit(
        self,
        identity: Identity,
        layers: Sequence[IdentityLayer],
        *,
        up_to_version_id: int,
        via: str,
    ) -> None:
        """用户在运行中改了设定（网页设置页，D63 第 4 条）：下一次回复就用新设定。

        要在事件循环里调用（网页服务和心跳在同一个循环里）。内容真的变了的层记一条时间线，
        metadata 带 via，并写明记到了哪个版本，下次醒来就不会重复通知；只切换了锁定开关时
        layers 是空的，只换身份，不记。先换身份再记时间线：记不进去时，设定照样生效。

        Args:
            identity: 改完之后生效的身份。
            layers: 内容真的变了的层。
            up_to_version_id: 记到了哪个版本。
            via: 从哪里改的，比如 web。
        """
        self._identity = identity
        if not layers:
            return
        names = "、".join(layer.describe() for layer in layers)
        self._live_identity_changes = list(layers)
        self._timeline.append(
            event_type=EVENT_IDENTITY_CHANGED,
            content=f"用户修改了我的设定：{names}。",
            actor_type=ActorType.USER,
            visibility=VisibilityScope.PRIVATE,
            metadata={
                "layers": [layer.value for layer in layers],
                "up_to_version_id": up_to_version_id,
                "via": via,
            },
        )
        logger.info("用户通过 %s 修改了设定：%s", via, names)

    def halt(self, reason: str = "") -> bool:
        """设急停，并叫醒心跳尽快跳一拍。

        Returns:
            这次新设的返回 True；本来就在急停中返回 False。

        Raises:
            OSError: 标记文件写不进去，急停没有生效。
        """
        engaged = self._emergency_stop.engage(reason)
        if engaged:
            # 先停推送，再清理：取消分段时写的记录也不该推给网页。
            self.live_events.pause()
            self._sticker_followups.clear()
            self._reply_outbox.clear()
            self._cancel_indexing()
        self._wake.set()
        return engaged

    def resume(self) -> bool:
        """解除急停，并叫醒心跳尽快跳一拍。

        Returns:
            这次解除的返回 True；本来就不在急停中返回 False。

        Raises:
            OSError: 标记文件删不掉，急停还在。
        """
        released = self._emergency_stop.release()
        if released:
            self._indexing_retry_at = None
            if not self._stop_requested and not self._maintenance_paused:
                self.live_events.resume()
        self._wake.set()
        return released

    def describe_state(self) -> str:
        """此刻的状态，几行中文，给终端的 /state 之类的观察命令用。"""
        state = self._cognitive_state
        lines = [state.self_awareness_statement() if state else "还没有跳过第一拍。"]
        if state is not None:
            energy = (
                "精力 不限（没设月度上限）"
                if state.energy_level is None
                else f"精力 {state.energy_level:.0%}（{state.energy_state}档）"
            )
            lines.append(f"{energy}，信心 {state.confidence_note}")
        lines.extend(self._describe_signals())
        levels = self._hormone_levels()
        if levels is not None:
            lines.append(
                "激素："
                + "，".join(f"{_HORMONE_NAMES[kind]} {level:.2f}" for kind, level in levels.items())
            )
        agenda_line = self._describe_agenda_counts()
        if agenda_line:
            lines.append(agenda_line)
        mood = self._read_mood(None, viewer_is_user=True)
        if mood is not None:
            caution = "，谨慎中" if mood.cautious else ""
            lines.append(f"心情 {mood.valence:+.2f}{caution}")
        if self._thought is not None and self._thought.task.get_name() != language_runner.TASK_NAME:
            lines.append(f"正在想怎么回复 {len(self._thought.replying_to)} 条消息。")
        if self._language is not None:
            lines.append(
                f"语言学习：{self._language_last_status}；{self._language_pending()} 条待处理。"
            )
        if self._halt_status.halted:
            lines.append(f"急停中：{self._halt_status.reason}。收到的消息只记录，不回复。")
        waiting = len(self._unanswered) + len(self._pending)
        if waiting:
            lines.append(f"还有 {waiting} 条消息在排队。")
        if self._downtime is not None:
            lines.append(f"这次醒来：{self._downtime.describe(self._timezone)}")
        return "\n".join(lines)

    def context_message_limit(self) -> int:
        """此刻回复会带多少条最近对话，与实际回复共用档位和压力降档规则（D84）。"""
        return self._current_strategy().message_limit

    def working_memory_summaries(self) -> dict[str, str]:
        """读取本次运行已恢复的分区摘要，供管理网页展示。"""
        return self._working_memory.summaries() if self._working_memory is not None else {}

    def language_learning_status(self) -> dict[str, object]:
        """只读的后台学习状态；不包含消息正文或会话标识。"""
        return {
            "pending": self._language_pending(),
            "dropped": sum(
                learner.pending.dropped_count for learner in self._language_learners.values()
            ),
            "status": self._language_last_status,
            "next_at": next_at.isoformat() if (next_at := self._language_due_at()) else None,
        }

    def working_memory_capacity(self) -> int | None:
        """工作记忆启用时返回生效的配置容量。"""
        return self._config.working_memory_capacity if self._config.working_memory_enabled else None

    def working_memory_focus(self) -> tuple[int, int | None] | None:
        """管理网页所见的工作记忆容量和最早焦点；停用时沿用对话窗口。"""
        if (
            self._working_memory is None
            or not self._config.working_memory_enabled
            or not self._working_memory.is_ready
        ):
            return None
        return self._config.working_memory_capacity, self._working_memory.focus_boundary()

    def _describe_signals(self) -> list[str]:
        """疲劳、压力、夜间的一行读数；关掉的信号注明已关闭。"""
        reading = self._reading
        if reading is None:
            return []
        parts: list[str] = []
        fatigue, stress = reading.fatigue, reading.stress
        if fatigue.enabled:
            parts.append(f"疲劳 {fatigue.level:.0%}" + ("（偏高）" if fatigue.high else ""))
        if stress.enabled:
            parts.append(f"压力 {stress.level:.0%}" + ("（偏高）" if stress.high else ""))
        if reading.sleep.enabled and reading.sleep.night:
            parts.append("夜间时段")
        return ["，".join(parts)] if parts else []

    @property
    def downtime(self) -> Downtime | None:
        """这次醒来前停了多久；还没醒来时是 None。"""
        return self._downtime

    @property
    def identity(self) -> Identity:
        """此刻生效的身份；用户在网页上改了设定后，这里就是改过之后的（D63）。"""
        return self._identity

    def bot_names(self) -> tuple[str, ...]:
        """当前身份的全名和昵称；通道据此判断正文是否点名（D137）。"""
        return (self._identity.facts.name, *self._identity.facts.alias_names)

    @property
    def bot_pronoun(self) -> str:
        """显示当前身份的称谓。"""
        return self._identity.facts.pronoun

    @property
    def cognitive_state(self) -> CognitiveState | None:
        """最近一拍广播的认知状态；还没跳过时是 None。"""
        return self._cognitive_state

    @property
    def is_thinking(self) -> bool:
        """是否有进行中的想法（结果还没被处理）。"""
        return self._thought is not None

    def is_replying_to(
        self,
        *,
        conversation: ConversationKey | None = None,
        web_visibility: VisibilityScope | None = None,
    ) -> bool:
        """只向指定平台原件会话或认证网页私聊报告正在进行的回复。"""
        thought = self._thought
        if (
            thought is None
            or not thought.replying_to
            or thought.task.done()
            or thought.task.cancelling()
            or thought.inbox_discard_reason is not None
            or self._stop_requested
            or self._halt_status.halted
        ):
            return False
        heard = thought.replying_to[-1]
        target = heard.perception
        if conversation is not None:
            inbox = self._platform_inbox
            return (
                target.visibility is not VisibilityScope.PRIVATE
                and _platform_key(heard) == conversation
                and (
                    heard.residence is None
                    or (inbox is not None and inbox.permits(heard.residence))
                )
            )
        return (
            target.channel == WEB_CHANNEL_NAME
            and target.sender_id == WEB_USER_ID
            and target.trust is TrustLevel.VERIFIED_USER
            and target.route is None
            and web_visibility is VisibilityScope.USER_ONLY
            and target.visibility is web_visibility
        )

    # ---------- 生命周期 ----------

    async def run(self) -> None:
        """醒来，一直跳到 request_stop()，然后关闭。被取消时也会先走完关闭流程再退出。"""
        self.wake_up()
        reason = SHUTDOWN_REQUESTED
        try:
            while not self._stop_requested:
                self.beat()
                await self._wait_for_next_beat()
        except asyncio.CancelledError:
            reason = SHUTDOWN_INTERRUPTED
            raise
        except Exception:
            # 出错时停下来，而不是带着没记全的经历继续跑（时间线是唯一的经历记录）。
            reason = SHUTDOWN_AFTER_ERROR
            logger.exception("心跳出错，开始关闭")
            raise
        finally:
            await self.shut_down(reason)

    def request_stop(self) -> None:
        """请心跳停下来：跳完当前这一拍就开始关闭。"""
        self._stop_requested = True
        self.live_events.pause()
        self._cancel_language_task()
        self._cancel_indexing()
        if self._platform_inbox is not None:
            self._interrupt_inbox(self._platform_inbox, "关闭时取消本批回复，未投递。")
        self._sticker_followups.clear()
        self._reply_outbox.clear()
        self._wake.set()

    @property
    def maintenance_paused(self) -> bool:
        """网页总览是否已请求关机。"""
        return self._maintenance_paused

    def set_maintenance_paused(self, paused: bool) -> None:
        """在同一进程内关停活动，保留网页控制和平台收件。"""
        if paused == self._maintenance_paused:
            return
        self._maintenance_paused = paused
        # 先停推送，再取消进行中的回复：取消时写的记录也不该推给网页。
        self._update_live_pause()
        if paused:
            self._cancel_indexing()
            self._inbox_followups.clear()
            self._sticker_followups.clear()
            self._reply_outbox.clear()
            if self._platform_inbox is not None:
                self._interrupt_inbox(self._platform_inbox, "维护暂停时取消本批回复，未投递。")
        else:
            self._indexing_retry_at = None
        if paused and self._thought is not None:
            thought = self._thought
            if thought.round_trace:
                thought.round_trace.cancel_sticker()
            is_language_learning = thought.task.get_name() == language_runner.TASK_NAME
            self._thought = None
            self._maintenance_task = thought.task
            if is_language_learning:
                thought.task.cancel()
            else:
                self._record_interrupted_round(
                    thought, thought.inbox_discard_reason or "维护暂停时取消本轮回复，未投递。"
                )
                if thought.inbox_discard_reason is None:
                    thought.task.cancel()
                    self._requeue_reply(thought)
        self._wake.set()

    def wake_up(self) -> Downtime:
        """醒来：算出停了多久，记进时间线。

        Returns:
            停机情况。

        Raises:
            RuntimeError: 已经醒来过了。
        """
        if self._phase is not _Phase.NOT_STARTED:
            raise RuntimeError("心跳已经醒来过了，一个心跳对象只能运行一次")
        now = self._clock.now()
        latest = self._timeline.query(TimelineQuery(limit=1, latest=True))
        downtime = assess_downtime(latest[0] if latest else None, now)
        if self._working_memory is not None:
            self._working_memory.restore()
        self._timeline.append(
            event_type=EVENT_WOKE_UP,
            content=f"醒来了。{downtime.describe(self._timezone)}",
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata=downtime.to_metadata(),
        )
        # 写在 woke_up 之后：停机时长看的是醒来前的最后一条记录，不能被这条挡住。
        self._record_identity_changes()
        self._restore_hormones(now)
        self._restore_last_conversation()
        self._initiative.restore(self._clock.now())
        if self._platform_inbox is not None:
            self._platform_inbox.restore()
        if self._diary is not None:
            self._diary.restore(now)
        if self._persona_growth is not None:
            self._persona_growth.restore(now)
        self._downtime = downtime
        self._phase = _Phase.RUNNING
        logger.info("心跳醒来：%s", downtime.describe(self._timezone))
        return downtime

    def _restore_hormones(self, now: datetime) -> None:
        """醒来时从时间线上的触发记录还原激素浓度；停机的时间照样算作消退（D69 第 5 条）。"""
        if self._hormones is None and self._mood is None:
            return
        records = self._timeline.query(
            TimelineQuery(
                event_type=EVENT_HORMONE_TRIGGERED, limit=_HORMONE_RESTORE_LIMIT, latest=True
            )
        )
        metadata = [record.metadata for record in records]
        if self._hormones is not None:
            restored = self._hormones.restore(metadata, now)
            if restored:
                logger.info("还原了 %s 条还没消退的激素触发", restored)
        if self._mood is not None:
            # 心情的半衰期比激素长得多，同一批记录里激素已经消退的，心情还记着（D73 第 3 条）。
            self._mood.restore(metadata, now)

    def _restore_last_conversation(self) -> bool:
        """恢复社交需求的起点；读失败时本拍不主动。"""
        try:
            self._last_conversation_at = read_last_conversation(self._timeline)
            return True
        except Exception:
            logger.exception("上次有效对话读不了，不凭空当作刚聊过")
            return False

    def _record_identity_changes(self) -> None:
        """醒来时：用户在 identity.toml 里改过设定的话，记一条「用户修改了我的设定」（D62）。

        从上一条这种记录写下的版本号之后找新的用户修改，所以每次修改只记一次；
        上次改完没来得及记就停了机，这次醒来也会补上。
        """
        if self._identity_history is None:
            return
        changes = self._identity_history.user_changes_after(self._announced_version())
        if not changes:
            return
        layers = list(dict.fromkeys(change.layer for change in changes))
        names = "、".join(layer.describe() for layer in layers)
        self._timeline.append(
            event_type=EVENT_IDENTITY_CHANGED,
            content=f"用户修改了我的设定：{names}。",
            actor_type=ActorType.USER,
            visibility=VisibilityScope.PRIVATE,
            metadata={
                "layers": [layer.value for layer in layers],
                "version_ids": [change.id for change in changes],
                "up_to_version_id": changes[-1].id,
            },
        )
        self._identity_changes = layers
        logger.info("用户修改了设定：%s", names)

    def _announced_version(self) -> int:
        """上一条「用户修改了我的设定」记到了哪个版本；没有、或者读不懂时是 0。

        读不懂时从头找：最多把以前的修改再说一遍，不会漏掉。
        """
        latest = self._timeline.query(
            TimelineQuery(event_type=EVENT_IDENTITY_CHANGED, limit=1, latest=True)
        )
        if not latest:
            return 0
        value = latest[0].metadata.get("up_to_version_id")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        logger.warning("上一条设定修改记录里的版本号读不懂，从头找用户的修改")
        return 0

    def beat(self) -> None:
        """跳一拍。只做很短的同步操作；要在事件循环里调用（会创建后台任务）。

        Raises:
            RuntimeError: 心跳没有在运行。
        """
        if self._phase is not _Phase.RUNNING:
            raise RuntimeError("心跳没有在运行，不能跳")
        pending_cancel = self._maintenance_task
        if pending_cancel is not None and pending_cancel.done():
            if not pending_cancel.cancelled():
                pending_cancel.exception()
            self._maintenance_task = None
        if self._maintenance_paused or self._maintenance_task is not None:
            return
        if self._emoji_maintenance is not None:
            self._emoji_maintenance.maybe_start(self._clock.now())
        self._initiative.begin_beat(self._clock.now())
        self._exploration.begin_beat(self._clock.now())
        self._watch_emergency_stop()
        inbox = self._platform_inbox
        if inbox is not None:
            self._take_inbox_priority(inbox)
        # D76：先收下已到的来信，不能把发送前排队的消息算作发送后的回应。
        self._take_in_perceptions()
        if inbox is not None:
            self._take_inbox_followups(inbox)
        self._sticker_followups.tick()
        self._reply_outbox.deliver_due()
        had_thought = self._thought is not None
        diary_running = bool(self._thought and self._thought.task.get_name() == "aureus-diary")
        self._collect_finished_thought()
        self._collect_finished_judgments()
        if inbox is not None:
            self._take_active_inbox(inbox)
        self._memory.index_more()
        self._maybe_start_indexing()
        self._read_body()
        self._check_stress()
        self._agenda_hooks._check_agenda()
        expired = inbox is not None and self._expire_inbox_residence(inbox)
        self._maybe_recheck_chat()
        if inbox is not None:
            self._maybe_select_inbox(inbox, had_thought=had_thought and not expired)
        thought = self._thought
        if (
            self._unanswered
            and thought is not None
            and thought.task.get_name() == WORKING_MEMORY_TASK
            and any(self._is_bound_user(heard.perception) for heard in self._unanswered)
        ):
            thought.task.cancel()
        self._maybe_start_thought()
        self._maybe_summarize_working_memory()
        self._maybe_learn_language()
        self._maybe_start_initiative()
        self._maybe_start_exploration()
        self._maybe_start_diary()
        if not diary_running:
            self._maybe_start_persona_review()
        self._maybe_consolidate_memory()
        self._broadcast_cognitive_state()
        self._record_body_sample()
        self._publish_round_state()

    async def wait_for_thought(self) -> None:
        """等进行中的想法有结果（结果由下一拍处理）。没有在想就立即返回。"""
        thought = self._thought
        if thought is not None:
            await asyncio.wait({thought.task})

    async def shut_down(self, reason: str = SHUTDOWN_REQUESTED) -> None:
        """关闭：不再开始新的想法，等进行中的想法一会儿，把没记的消息记下来，写关闭记录。

        尽力而为：写不进时间线时只记日志，不抛异常，免得掩盖让心跳停下来的那个原始错误。
        关闭记录没写上也不要紧，下次醒来会按“没有正常关闭”算停机时长，结论仍然成立。

        Args:
            reason: 为什么关闭，写进关闭记录。
        """
        if self._phase is _Phase.NOT_STARTED:
            # 没醒来过就没有什么可收尾的；也不写关闭记录，免得下次醒来误以为运行过。
            self._phase = _Phase.STOPPED
            return
        if self._phase is not _Phase.RUNNING:
            return
        self._phase = _Phase.STOPPING
        # 也叫醒主循环：如果 run() 正在等下一拍，它会马上发现要停，而不是等到定时拍。
        self.request_stop()
        try:
            try:
                await self._settle_thought_before_shutdown()
                await self._settle_judgments_before_shutdown()
                self._take_in_perceptions()
                if self._emoji_maintenance is not None:
                    await self._emoji_maintenance.wait()
            except Exception:
                logger.exception("关闭时没能把进行中的想法或排队的消息记进时间线")
            try:
                self._timeline.append(
                    event_type=EVENT_SHUT_DOWN,
                    content=f"关闭：{reason}。",
                    actor_type=ActorType.ME,
                    visibility=VisibilityScope.PRIVATE,
                    metadata={"reason": reason, "unanswered_messages": len(self._unanswered)},
                )
            except Exception:
                logger.exception("关闭记录没能写进时间线")
        finally:
            await self._wait_for_indexing()
            if self._conversation.expression_recall is not None:
                await self._conversation.expression_recall.close()
            self._phase = _Phase.STOPPED
        logger.info("心跳已关闭：%s", reason)

    # ---------- 每一拍的步骤 ----------

    def _watch_emergency_stop(self) -> None:
        """读急停状态：变了就记进时间线；急停中就取消进行中的器官调用（D54）。"""
        status = self._emergency_stop.status()
        previous = self._halt_status
        self._halt_status = status
        self._update_live_pause()
        if status.halted:
            self._sticker_followups.clear()
            self._reply_outbox.clear()
            if self._platform_inbox is not None:
                self._interrupt_inbox(self._platform_inbox, "急停时取消本批回复，未投递。")
            self._invalidate_all_judgments()
        if status.halted != previous.halted:
            self._timeline.append(
                event_type=EVENT_EMERGENCY_STOP if status.halted else EVENT_EMERGENCY_RESUME,
                content=f"急停生效：{status.reason}" if status.halted else "急停解除",
                actor_type=ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata={
                    "reason": status.reason,
                    "since": status.since.isoformat() if status.since else "",
                },
            )
            logger.warning("急停%s", f"生效：{status.reason}" if status.halted else "解除")
        thought = self._thought
        if (
            status.halted
            and thought is not None
            and not thought.task.done()
            and thought.inbox_discard_reason is None
        ):
            # 取消的结果由 _collect_finished_thought 在下一拍处理（取消完成时会叫醒心跳）。
            thought.task.cancel()

    def _collect_finished_thought(self) -> None:
        """对结果：想法有结果了，把它说的话记进时间线、发回通道；失败就发系统通知。"""
        thought = self._thought
        if thought is None or not thought.task.done():
            return
        self._thought = None
        try:
            self._collect_outcome(thought)
        finally:
            if thought.round_trace:
                thought.round_trace.cancel_sticker()

    def _collect_outcome(self, thought: _Thought) -> None:
        inbox = self._platform_inbox
        residence = thought.replying_to[-1].residence if thought.replying_to else None
        lost_permission = residence is not None and (inbox is None or not inbox.permits(residence))
        if thought.inbox_discard_reason is not None or lost_permission:
            if lost_permission:
                self._clear_planner_restarts(self._thought_conversation(thought))
            reason = thought.inbox_discard_reason or "会话已失去阅读权限，未投递。"
            self._record_interrupted_round(thought, reason)
            if thought.initiative is not None:
                self._collect_initiative(thought, _ThoughtOutcome(notice=reason))
            if not thought.task.cancelled():
                thought.task.exception()
            return
        if thought.planner_run is not None and thought.planner_run.restart_requested:
            self._record_interrupted_round(thought, "新消息中断了本次规划，未投递。")
            if not self._halt_status.halted:
                self._requeue_reply(thought)
            if not thought.task.cancelled():
                thought.task.exception()
            return
        if thought.task.cancelled():
            # 先看是不是被取消的：急停取消不该清掉重开上限（D138）。
            self._record_abandoned(thought, halted=self._halt_status.halted)
            return
        if (conversation := self._thought_conversation(thought)) is not None:
            self._planner_restarts.pop(conversation, None)
        if thought.task.get_name() == language_runner.TASK_NAME:
            return
        outcome = thought.task.result()
        assert outcome is not None and not isinstance(outcome, bool)
        if isinstance(outcome, InboxSelection):
            if (
                inbox is not None
                and outcome.conversation is not None
                and outcome.conversation.platform in self._sinks
                and not self._unanswered
                and self.allows_emoji_delivery()
                and self._permissions.check(ACTION_CALL_ORGAN).allowed
            ):
                self._submit_inbox(inbox.read_selection(outcome, now=self._clock.now()))
                self._take_in_perceptions(browsing=True, residence=inbox.residence)
            return
        if isinstance(outcome, SummaryOutcome):
            assert self._working_memory is not None
            self._collect_trimmed_learning(self._working_memory.finish(outcome, self._clock.now()))
            return
        if isinstance(outcome, ConsolidationOutcome):
            assert self._memory_consolidator is not None
            self._memory_consolidator.finish(outcome, self._clock.now())
            if outcome.completed:
                self.live_events.publish(LiveEvent(TOPIC_RESOURCES, {"resource": RESOURCE_MEMORY}))
            return
        if isinstance(outcome, growth.PersonaReviewOutcome):
            assert self._persona_growth is not None
            self._identity = self._persona_growth.finish(outcome, self._identity) or self._identity
            return
        if isinstance(outcome, DiaryOutcome):
            assert self._diary is not None
            self._diary.finish(outcome)
            return
        if isinstance(outcome, exploration_runner.ExplorationOutcome):
            self._exploration.finish(cast(TimelineRecord, thought.exploration), outcome)
            return
        if thought.initiative is not None:
            self._collect_initiative(thought, outcome)
            return
        selected = thought.replying_to[-1]
        if outcome.target_record_id is not None:
            candidate = next(
                (
                    heard
                    for heard in thought.replying_to
                    if heard.record.id == outcome.target_record_id
                ),
                None,
            )
            if candidate is None:
                return
            selected = candidate
        elif (
            outcome.notice and len(thought.replying_to) > 1 and _platform_key(selected) is not None
        ):
            return
        target, target_key = selected.perception, _platform_key(selected)
        if not self._conversation.source_available(
            selected
        ) or not self._conversation.history_current(thought.round_trace):
            self._record_interrupted_round(thought, "回复原件已不可用，未投递。")
            return
        if target_key is not None and inbox is not None and not inbox.allows(target_key):
            self._clear_planner_restarts(target_key)
            self._record_interrupted_round(thought, "会话已失去阅读权限，未投递。")
            return
        trace, retention = thought.round_trace, self.history_retention
        if trace and not self._chat_round_current(trace, selected):
            self._record_interrupted_round(thought, "回看已被新来信替代或叮嘱已失效，不投递。")
            return
        used_platform_history = trace is not None and trace.used_platform_history
        if used_platform_history:
            plan = outcome.plan
            if plan is not None:
                outcome = replace(outcome, plan=replace(plan, reason=""))
        if trace is not None and retention is not None and not self._halt_status.halted:
            preview, history = trace.cleanup_preview, self._conversation.platform_history
            if preview is not None and history is not None and history.permits(preview.scope.key):
                retention.publish(preview)
        person_id = _person_id_of(selected.record)
        if inbox is not None and outcome.plan is not None and not self._halt_status.halted:
            plan = outcome.plan
            queued = self._unanswered.peek_batch()
            if plan.stay_in_conversation and not (
                self._permissions.check(ACTION_CALL_ORGAN).allowed and self.allows_emoji_delivery()
            ):
                plan = replace(plan, stay_in_conversation=None)
            elif (
                target_key is not None
                and not self._unanswered.peek_batch((None, target_key))
                and any(
                    _platform_key(heard) not in (None, target_key)
                    and inbox.yields_to_when_idle(heard.perception)
                    for heard in queued
                )
            ):
                plan = replace(plan, stay_in_conversation=False)
            was_active = inbox.active_conversation == target_key and target_key is not None
            inbox.apply_plan(plan, target, selected.record, now=self._clock.now())
            if inbox.residence is not None and inbox.residence.conversation == target_key:
                residence = inbox.residence
                if was_active and inbox.active_conversation is None:
                    self._discard_residence_batches(residence, "已离开会话，未开始这批规划。")
        self._record_reply_reference(thought, outcome.reference_plan, target)
        if outcome.declined:
            if (
                outcome.plan
                and outcome.plan.wait_seconds
                and trace
                and not trace.timed_chat
                and self.allows_emoji_delivery()
                and (chat := self._conversation.chat_plugins)
            ):
                chat.recheck("wait", target, outcome.plan.wait_seconds)
            if outcome.plan is not None:
                if self._hormones is not None:
                    self._hormones.note_decline(person_id, self._clock.now())
                self._record_declined(thought, outcome.plan, outcome.effects)
            return
        if outcome.response is None and outcome.emoji is None and outcome.qq_action is None:
            if outcome.notice:
                self._deliver(target, outcome.notice, MessageKind.NOTICE)
            return
        segments = outcome.reply_segments
        if not segments and outcome.emoji is None and outcome.qq_action is None:
            return
        reply_permission = self._permissions.check(ACTION_REPLY_TO_USER)
        if not reply_permission.allowed:
            # 器官已经返回了，但此刻不允许说话：这句话不进时间线（它没有说出口）。
            logger.warning("回复被权限检查拦下：%s", reply_permission.reason)
            self._record_round_delivery_block(thought, "回复权限检查未通过，未投递。")
            self._deliver(
                target,
                f"{reply_permission.reason}。这条消息已经记下，没有回复。",
                MessageKind.NOTICE,
            )
            return
        text = "\n".join(segments)
        metadata: dict[str, Any] = {
            "channel": target.channel,
            **self._round_metadata(thought),
            INTERNAL_REPLY_RECORD: True,
            "organ_id": outcome.response.organ_id if outcome.response is not None else "planner",
            "strategy": thought.strategy.to_metadata(),
        }
        if outcome.target_record_id is not None:
            metadata["in_reply_to"] = [selected.record.id]
        if not segments:
            metadata["nontext_reply"] = True
        if outcome.qq_action is not None:
            metadata["qq_action"] = outcome.qq_action.kind
        if outcome.plan and outcome.plan.reply_mode in ("repeat", "sticker_duel"):
            metadata["chat_play"] = True
        if len(segments) > 1:
            metadata["reply_segments"] = list(segments)
        if trace and trace.owner_nudge:
            metadata["chat_nudge_id"] = trace.owner_nudge.id
        if trace and not trace.timed_chat and outcome.plan and outcome.plan.followup_seconds:
            metadata["followup_seconds"] = outcome.plan.followup_seconds
        if outcome.plan is not None and not used_platform_history:
            metadata["plan"] = outcome.plan.to_metadata()
        if outcome.recall_keywords:
            metadata["recall_keywords"] = list(outcome.recall_keywords)
            metadata["recall_record_ids"] = list(outcome.recall_record_ids)
        if outcome.recall_source_keys:
            metadata["recall_source_keys"] = list(outcome.recall_source_keys)
        if outcome.recall_feedback_refs:
            metadata["recall_feedback_refs"] = list(outcome.recall_feedback_refs)
        if outcome.effects is not None:
            metadata["hormones"] = outcome.effects.to_metadata()
            if self._hormones is not None:
                retaliated = outcome.effects.retaliation is RetaliationMode.ALLOWED
                self._hormones.note_reply(person_id, self._clock.now(), retaliated=retaliated)
        if target.trust is TrustLevel.VERIFIED_USER:
            metadata["to_user"] = True
        if target.route is not None:
            metadata.update(
                account_id=target.route.account_id,
                recipient_id=target.route.recipient_id,
                message_type=target.route.kind,
                temporary_group_id=target.route.temporary_group_id,
            )
        reply_record = self._timeline.append(
            event_type=EVENT_MESSAGE,
            content=text,
            actor_type=ActorType.ME,
            visibility=target.visibility,
            metadata=metadata,
        )

        def can_deliver() -> bool:
            return (
                (residence is None or (inbox is not None and inbox.permits(residence)))
                and (target_key is None or inbox is None or inbox.allows(target_key))
                and self._conversation.source_available(selected)
                and self._conversation.history_current(thought.round_trace)
                and (trace is None or self._chat_round_current(trace, selected, reply_record.id))
                and self.allows_emoji_delivery()
            )

        receipt: PartReceipt | None = None
        source_ids = [
            identifier
            for heard in thought.replying_to
            if type(identifier := heard.record.metadata.get("inbox_message_id")) is int
        ]
        if (
            segments
            and target_key is not None
            and inbox is not None
            and outcome.emoji is None
            and (outcome.emoji_task is not None or outcome.after_text_selection is not None)
            and source_ids
            and len(source_ids) == len(thought.replying_to)
        ):
            through = max(source_ids)
            deadline = self._clock.now() + timedelta(seconds=self._config.qq_emoji_followup_seconds)

            def can_add_sticker() -> bool:
                return (
                    self._clock.now() < deadline
                    and can_deliver()
                    and not inbox.store.has_newer_incoming(target_key, through)
                )

            def send_sticker(emoji: EmojiReplyPart) -> None:
                self._deliver_reply(
                    target, reply_record, (), emoji=emoji, can_deliver=can_add_sticker
                )

            receipt = self._sticker_followups.add(
                target_key,
                StickerRound(
                    deadline,
                    can_add_sticker,
                    send_sticker,
                    outcome.after_text_selection,
                    outcome.emoji_task,
                    len(segments),
                ),
            )
            if trace is not None:
                trace.sticker_selection = None
        elif outcome.emoji_task is not None:
            outcome.emoji_task.cancel()

        acknowledged = False

        def after_receipt(segment: int | None, accepted: bool) -> None:
            nonlocal acknowledged
            if receipt is not None:
                receipt(segment, accepted)
            if accepted and segment is not None and not acknowledged:
                acknowledged = True
                self._after_reply_delivery(selected, outcome.plan, person_id, reply_record)

        self._deliver_reply(
            target,
            reply_record,
            segments,
            emoji=outcome.emoji,
            qq_action=outcome.qq_action,
            quote_source=outcome.plan is not None and outcome.plan.quote_source,
            can_deliver=can_deliver if residence is not None or target_key is not None else None,
            on_receipt=after_receipt,
        )

    def allows_emoji_delivery(self) -> bool:
        """通道实际发送表情前再次核对暂停、停机和回复权限。"""
        return (
            not self._maintenance_paused
            and not self._stop_requested
            and self._permissions.check(ACTION_REPLY_TO_USER).allowed
        )

    def _send_reply_part(
        self,
        target: Perception,
        part: str | EmojiReplyPart | QQChatAction | AtReplyPart,
        record: TimelineRecord,
        quote_source: bool,
        segment: int | None,
        can_deliver: Callable[[], bool] | None,
        on_receipt: PartReceipt | None = None,
    ) -> bool:
        action = (
            part.action
            if isinstance(part, AtReplyPart)
            else part
            if isinstance(part, QQChatAction)
            else None
        )
        text = part if isinstance(part, str) else part.text if isinstance(part, AtReplyPart) else ""
        descriptions = {
            "face": f"[QQ表情#{action.emoji_id}]" if action else "",
            "react": "已给指定消息贴表情。",
            "poke": "已戳了对方一下。",
            "recall": "已撤回指定的己方消息。",
            "resend_image": "[重发原图]",
            "send_asset": "[已发送受托素材]",
        }
        content = text or (descriptions.get(action.kind, "") if action else "发送了表情。")

        def confirmed() -> None:
            if on_receipt is not None:
                on_receipt(segment, True)
            if segment is not None and (
                "chat_nudge_id" in record.metadata or "followup_seconds" in record.metadata
            ):
                self._chat_part_confirmed(target, record)

        confirm, fail = delivery_callbacks(
            self._timeline,
            record,
            content,
            segment,
            on_confirm=confirmed,
            on_failure=(lambda: on_receipt(segment, False)) if on_receipt else None,
        )
        permission = self._permissions.check(ACTION_REPLY_TO_USER)
        if not permission.allowed:
            logger.warning("待发回复被权限检查拦下：%s", permission.reason)
            fail("回复权限检查未通过，未投递。")
            return False
        if self._maintenance_paused:
            fail("维护暂停中，未投递。")
            return False
        if can_deliver is not None and not can_deliver():
            fail("会话已切换或不再允许发送，未投递。")
            return False
        quote_source = quote_source and self._can_quote_reply(target)
        is_emoji = isinstance(part, EmojiReplyPart)
        if (is_emoji or action is not None) and (
            target.channel != "qq" or target.route is None or self._stop_requested
        ):
            fail("表情不满足发送条件，未投递。")
            return False
        delivered = self._deliver(
            target,
            text,
            MessageKind.REPLY,
            record,
            emoji_digest=part.digest if isinstance(part, EmojiReplyPart) else None,
            qq_action=action,
            quote_source=quote_source,
            confirm_delivery=confirm,
            confirm_platform_delivery=confirm,
            report_delivery_failure=fail,
            reply_segment=segment,
            can_deliver=can_deliver,
        )
        if not delivered:
            fail("通道没有接收这段回复，未投递。")
        return delivered

    def _chat_part_confirmed(self, target: Perception, record: TimelineRecord) -> None:
        chat = self._conversation.chat_plugins
        if not chat or not target.route:
            return
        if nudge_id := record.metadata.get("chat_nudge_id"):
            chat.consume_nudge(ConversationKey.from_perception(target), nudge_id)
        if seconds := record.metadata.get("followup_seconds"):
            total = len(record.metadata.get("reply_segments") or [record.content])
            confirmed = self._timeline.query(
                TimelineQuery(
                    event_type=EVENT_REPLY_DELIVERED, metadata={"reply_id": record.id}, limit=-1
                )
            )
            if {
                item.metadata.get("segment")
                for item in confirmed
                if item.metadata.get("segment") is not None
            } == set(range(1, total + 1)):
                chat.recheck("followup", target, seconds)

    def _after_reply_delivery(
        self,
        heard: _Heard,
        plan: Plan | None,
        person_id: int | None,
        reply_record: TimelineRecord,
    ) -> None:
        if heard.perception.trust is TrustLevel.VERIFIED_USER:
            self._last_conversation_at = self._clock.now()
        if plan is not None and person_id is not None:
            # 说出口了才算答应（D74 第 3 条）：规划器决定不回、或者没说出口时不记。
            self._agenda_hooks._apply_agenda(plan, person_id, reply_record)
            if plan.interest is not None and heard.is_user:
                interest_changes.apply_interest(
                    self._interests,
                    self._timeline,
                    self._exploration.calendar,
                    plan.interest,
                    heard.record,
                    self._clock.now(),
                )

    def _record_discarded_reply(self, record: TimelineRecord, segment: int | None) -> None:
        _, fail = delivery_callbacks(self._timeline, record, "", segment)
        fail("还在等待的回复分段已取消，未投递。")

    def _deliver_reply(
        self,
        target: Perception,
        record: TimelineRecord,
        parts: Sequence[str],
        after_delivery: Callable[[], None] | None = None,
        after_failure: Callable[[], None] | None = None,
        *,
        emoji: EmojiReplyPart | None = None,
        qq_action: QQChatAction | None = None,
        quote_source: bool = False,
        can_deliver: Callable[[], bool] | None = None,
        on_receipt: PartReceipt | None = None,
    ) -> bool:
        """投递已记录的各段。返回本次调用中是否已交给通道。"""
        if (
            self._stop_requested
            and self._config.typing_delay_enabled
            and can_deliver is None
            and parts
            and qq_action is None
        ):
            delivered = self._send_reply_part(
                target, parts[0], record, quote_source, 1, None, on_receipt
            )
            finish = after_delivery if delivered else after_failure
            if finish is not None:
                finish()
            return delivered
        inbox = self._platform_inbox
        residence = inbox.residence if inbox is not None else None
        if residence is not None and (
            target.route is None
            or ConversationKey.from_perception(target) != residence.conversation
        ):
            residence = None

        def renew_residence() -> None:
            if inbox is not None and residence is not None and inbox.permits(residence):
                inbox.renew(self._clock.now())

        return self._reply_outbox.enqueue(
            target,
            record,
            parts,
            after_delivery,
            after_failure,
            emoji=emoji,
            qq_action=qq_action,
            quote_source=quote_source,
            can_deliver=can_deliver,
            on_receipt=on_receipt,
            track_receipts=residence is not None and target.channel in self._receipt_channels,
            on_activity=renew_residence if residence is not None else None,
        )

    def _register_judgment(self, pending: _PendingJudgment) -> None:
        """收下后台判断的所有权，跑完时叫醒心跳来收取（D110）。"""
        self._pending_judgments.append(pending)
        pending.task.add_done_callback(self._on_thought_done)

    def _collect_finished_judgments(self) -> None:
        """收取跑完的判断；来源权限失效或轮次作废的迟到建议只丢弃，不写状态（D110）。"""
        if not self._pending_judgments:
            return
        remaining: list[_PendingJudgment] = []
        for pending in self._pending_judgments:
            if pending.invalidated or not self._judgment_source_allowed(pending):
                pending.invalidated = True
                if pending.task.done():
                    self._discard_judgment_result(pending)
                else:
                    pending.task.cancel()
                    remaining.append(pending)
                continue
            if not pending.task.done():
                remaining.append(pending)
                continue
            try:
                attempt = pending.task.result()
            except asyncio.CancelledError:
                continue
            except Exception:
                logger.exception("判断任务出错，丢弃这次结果")
                continue
            if attempt is not None:
                self._adopt_judgment(pending, attempt)
        self._pending_judgments = remaining

    def _adopt_judgment(self, pending: _PendingJudgment, attempt: _JudgeAttempt) -> None:
        """在心跳这一拍采纳判断：按采纳时的时钟算触发，再走既有接口写状态（D110）。"""
        triggers = (
            self._conversation.judgment_triggers(
                attempt.judgment, pending.messages, pending.person_id
            )
            if attempt.judgment is not None
            else ()
        )
        record = self._record_adopted_judgment(pending, attempt)
        self._record_triggers(triggers)
        self._record_affinity(pending.person_id, attempt.judgment, record)

    def _record_adopted_judgment(
        self, pending: _PendingJudgment, attempt: _JudgeAttempt
    ) -> TimelineRecord:
        """判断记录沿用原轮次的 round_id 和来源，允许晚于回复出现（D68、D110）。"""
        metadata: dict[str, Any] = {"in_reply_to": [record.id for record in pending.messages]}
        if pending.round_trace is not None:
            metadata["round_id"] = pending.round_trace.id
        if attempt.judgment is not None:
            content = "判断了刚收到的消息里的情绪。"
            metadata["judgment"] = attempt.judgment.to_metadata()
        else:
            content = f"没判断出刚收到的消息里的情绪：{attempt.failure}"
            metadata["failure"] = attempt.failure
        return self._timeline.append(
            event_type=EVENT_JUDGMENT,
            content=content,
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata=metadata,
        )

    def _judgment_source_allowed(self, pending: _PendingJudgment) -> bool:
        """原消息所在的收件会话是否仍有阅读权限；没有就不采纳迟到的判断。"""
        inbox = self._platform_inbox
        if not self._conversation.history_current(pending.round_trace):
            return False
        if pending.residence is not None:
            return inbox is not None and inbox.permits(pending.residence)
        if pending.source_key is not None and inbox is not None:
            return inbox.allows(pending.source_key)
        return True

    @staticmethod
    def _discard_judgment_result(pending: _PendingJudgment) -> None:
        """丢掉不要的结果，取走任务的异常，免得事件循环报“异常没有被接住”。"""
        if pending.task.cancelled():
            return
        with contextlib.suppress(Exception):
            pending.task.result()

    def _invalidate_judgments(
        self,
        *,
        round_trace: _RoundTrace | None = None,
        residence: InboxResidence | None = None,
    ) -> None:
        """轮次作废或来源撤权时，取消对应的判断并丢弃结果（D110）。"""
        if round_trace is None and residence is None:
            return
        for pending in self._pending_judgments:
            if pending.invalidated:
                continue
            if round_trace is not None and pending.round_trace is not round_trace:
                continue
            if residence is not None and pending.residence is not residence:
                continue
            pending.invalidated = True
            if not pending.task.done():
                pending.task.cancel()

    def _invalidate_all_judgments(self) -> None:
        """急停等全局原因：所有还挂着的判断都不再采纳。"""
        for pending in self._pending_judgments:
            pending.invalidated = True
            if not pending.task.done():
                pending.task.cancel()

    def _record_affinity(
        self, person_id: int | None, judgment: Judgment | None, source: TimelineRecord
    ) -> None:
        """按读懂了的判断记好感变化（D71 第 1、2 条）。判断失败、认不出是谁时不变。"""
        if self._affinity is None or judgment is None or person_id is None:
            return
        events = [
            AffinityEvent(_AFFINITY_REASONS[item.kind], item.intensity, item.confidence)
            for item in judgment.toward_me
        ]
        self._affinity.record_judgment(person_id, events, source.id)

    def _affinity_for_context(self, person_id: int | None, *, is_user: bool) -> str:
        """对眼前这个人的好感，放进系统上下文；没有好感账或认不出是谁时不放。"""
        if self._affinity is None or person_id is None:
            return ""
        return describe_affinity(self._affinity.current(person_id), is_user=is_user)

    def _record_triggers(self, triggers: Sequence[HormoneTrigger]) -> None:
        """普通触发写入基线；共情只留痕，由工作记忆焦点决定是否叠加。"""
        if self._hormones is None or not triggers:
            return
        baseline = [trigger for trigger in triggers if not trigger.is_empathy]
        for trigger in baseline:
            self._timeline.append(
                event_type=EVENT_HORMONE_TRIGGERED,
                content=f"{trigger.kind.value} 上涨 {trigger.rise:.2f}：{trigger.reason}",
                actor_type=ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata=trigger.to_metadata(),
            )
        if baseline:
            self._hormones.add(baseline, self._clock.now())
            if self._mood is not None:
                self._mood.add_triggers(baseline, self._clock.now())
        self._empathy_focus.record(trigger for trigger in triggers if trigger.is_empathy)

    def _read_mood(
        self, person_id: int | None, *, viewer_is_user: bool, strict: bool = False
    ) -> MoodReading | None:
        """此刻的心情（D73）：激素贡献在内存里，反馈结果从反馈表读，再减去此刻压力。

        反馈表读不了时当作没有结果，不让心情这一步拖垮回复。
        """
        read = self._prepare_mood(person_id, viewer_is_user=viewer_is_user, strict=strict)
        return read(self._clock.now(), self._empathy_focus.active()) if read is not None else None

    def _prepare_mood(
        self, person_id: int | None, *, viewer_is_user: bool, strict: bool = False
    ) -> Callable[[datetime, Sequence[HormoneTrigger]], MoodReading] | None:
        mood = self._mood
        if mood is None:
            return None
        now = self._clock.now()
        outcomes: list[FeedbackOutcome] = []
        if self._feedback is not None:
            try:
                records = self._feedback.outcomes_since(now - mood.lookback())
            except sqlite3.Error:
                if strict:
                    raise
                logger.exception("心情：反馈表读不了，这次不算反馈结果")
                records = []
            outcomes = [
                FeedbackOutcome(
                    at=record.outcome_at,
                    succeeded=record.outcome is Outcome.SUCCESS,
                    category=record.category,
                )
                for record in records
                if record.outcome_at is not None
            ]
        reading = self._reading
        stress = reading.stress.level if reading is not None and reading.stress.enabled else 0.0
        return mood.prepare_reading(
            now,
            outcomes,
            stress_level=stress,
            person_id=person_id,
            viewer_is_user=viewer_is_user,
        )

    def _record_reply_reference(
        self, thought: _Thought, plan: Plan | None, target: Perception
    ) -> None:
        metadata: dict[str, Any] = {
            "channel": target.channel,
            **self._round_metadata(thought),
        }
        if thought.round_trace is not None:
            metadata["actions"] = [action.to_metadata() for action in thought.round_trace.actions]
            metadata.update(thought.round_trace.attention_metadata())
        content = "这轮没有可用的规划建议。"
        if plan is not None:
            content = "建议回复" if plan.should_reply else "建议不回复"
            trace = thought.round_trace
            used_history = trace is not None and trace.used_platform_history
            if plan.should_reply and plan.intent and not used_history:
                content += f"：{plan.intent}"
            if plan.reason and not used_history:
                content += f"\n依据：{plan.reason}"
            if plan.length is not None and plan.should_reply:
                content += f"\n建议篇幅：{plan.length.value}"
        if target.route is not None:
            metadata.update(
                account_id=target.route.account_id,
                recipient_id=target.route.recipient_id,
                message_type=target.route.kind,
                temporary_group_id=target.route.temporary_group_id,
            )
        self._timeline.append(
            event_type=EVENT_REPLY_REFERENCE,
            content=content,
            actor_type=ActorType.ME,
            visibility=target.visibility,
            metadata=metadata,
        )

    @staticmethod
    def _round_metadata(thought: _Thought) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "in_reply_to": [heard.record.id for heard in thought.replying_to]
        }
        if thought.round_trace is not None:
            metadata["round_id"] = thought.round_trace.id
        return metadata

    def _record_interrupted_round(self, thought: _Thought, reason: str) -> None:
        trace = thought.round_trace
        if trace is None:
            return
        self._invalidate_judgments(round_trace=trace)
        target = thought.replying_to[-1].perception
        self._timeline.append(
            EVENT_PLANNING_INTERRUPTED,
            reason,
            ActorType.ME,
            visibility=target.visibility,
            metadata={
                **self._round_metadata(thought),
                "channel": target.channel,
                "actions": [action.to_metadata() for action in trace.actions],
                **trace.attention_metadata(),
            },
        )

    def _record_round_delivery_block(self, thought: _Thought, reason: str) -> None:
        target = thought.replying_to[-1].perception
        self._timeline.append(
            EVENT_REPLY_DELIVERY_FAILED,
            reason,
            ActorType.SYSTEM,
            visibility=target.visibility,
            metadata={**self._round_metadata(thought), "channel": target.channel},
        )

    def _record_declined(
        self,
        thought: _Thought,
        plan: Plan,
        effects: HormoneEffects | None,
    ) -> None:
        """规划器明确决定不回复：只在时间线记录，不向会话发送消息（D103）。"""
        reason = plan.reason or "觉得不需要回应"
        metadata: dict[str, Any] = {
            **self._round_metadata(thought),
            "plan": plan.to_metadata(),
            "strategy": thought.strategy.to_metadata(),
        }
        if effects is not None:
            # 写明当时的怒气，回头查「为什么没回」时分得清是冷处理还是别的原因（D69 第 3 条）。
            metadata["hormones"] = effects.to_metadata()
        self._timeline.append(
            event_type=EVENT_REPLY_DECLINED,
            content=f"看了刚收到的消息，决定不回复：{reason}",
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata=metadata,
        )

    def _submit_inbox(self, messages: Sequence[InboxMessage]) -> None:
        existing_ids = {message.id for _, message in self._pending if message is not None}
        self._pending.extend(
            (message.perception, message)
            for message in messages
            if message.perception.channel in self._sinks and message.id not in existing_ids
        )

    def _thought_conversation(self, thought: _Thought | None = None) -> ConversationKey | None:
        thought = self._thought if thought is None else thought
        return _platform_key(thought.replying_to[-1]) if thought and thought.replying_to else None

    def _inbox_followup_key(self) -> ConversationKey | None:
        inbox = self._platform_inbox
        if inbox is not None and inbox.active_conversation is not None:
            return inbox.active_conversation
        # 防抖开启时新来信并入同会话批次；关闭防抖但开着快速反应时仍要能打断本轮。
        if self._config.planner_message_steering or self._config.message_debounce_mode != "off":
            batch = self._unanswered.peek_batch()
            return self._thought_conversation() or (_platform_key(batch[-1]) if batch else None)
        return None

    def _debouncer_for(self, key: ConversationKey) -> MessageDebouncer:
        """按会话取防抖状态；每个会话单独计时（D142）。"""
        debouncer = self._debouncers.get(key)
        if debouncer is None:
            debouncer = MessageDebouncer()
            self._debouncers[key] = debouncer
        return debouncer

    def _observe_debounce(self, message: InboxMessage) -> None:
        """把一条新来信交给该会话的防抖计时；关闭档位时不等（D142）。"""
        mode = self._config.message_debounce_mode
        if mode == "off":
            return
        perception = message.perception
        is_bare_image, is_pure_emoji = _debounce_shape(perception)
        now = self._clock.now().timestamp()
        received_at = message.received_at if message.received_at is not None else message.timestamp
        self._debouncer_for(message.conversation).observe(
            author=perception.sender_id,
            is_bare_image=is_bare_image,
            is_pure_emoji=is_pure_emoji,
            at_bot=perception.mentioned,
            received_at=received_at.timestamp(),
            wall_time=now,
            now=now,
            mode=mode,
        )

    def _take_inbox_priority(self, inbox: InboxAttention) -> None:
        if inbox.residence is not None and not (
            inbox.permits(inbox.residence)
            and self._permissions.check(ACTION_CALL_ORGAN).allowed
            and self.allows_emoji_delivery()
        ):
            self._clear_planner_restarts(inbox.residence.conversation)
            self._interrupt_inbox(inbox, "会话已失去处理权限，未投递。")
        messages = inbox.priority_messages()
        for message in messages:
            if inbox.preempts(message.perception):
                self._preempt_inbox(inbox, message.conversation)
        self._submit_inbox(messages)
        # D145：优先来信单独处理，提交后按会话把未读水位推到最大入站消息；
        # 同会话更早或同时段的普通未读不再逐条追赶，也不会混进这一批优先回复。
        for conversation in dict.fromkeys(message.conversation for message in messages):
            inbox.store.read_latest_and_clear(conversation, limit=inbox.read_limit)

    def _preempt_inbox(self, inbox: InboxAttention, conversation: ConversationKey) -> None:
        reason = "绑定用户的来信抢占了注意力，未投递。"
        if inbox.residence is not None and conversation != inbox.residence.conversation:
            self._interrupt_inbox(inbox, reason)
        thought = self._thought
        if thought is not None and self._thought_conversation(thought) != conversation:
            if (
                thought.replying_to
                or thought.initiative is not None
                or thought.task.get_name() == SELECTION_TASK
            ):
                self._discard_inbox_thought(thought, reason)
            elif not thought.task.done():
                thought.task.cancel()

    def _interrupt_inbox(self, inbox: InboxAttention, reason: str) -> None:
        # 这里不清 _planner_restarts：抢占、切换、急停打断都不该重置重开上限，
        # 只有正常完成一轮才清零（D138）。失去阅读权限时由调用方单独清。
        residence = inbox.residence
        if residence is None:
            return
        inbox.interrupt()
        self._invalidate_judgments(residence=residence)
        self._discard_residence_batches(residence, reason)
        thought = self._thought
        if (
            thought is not None
            and thought.replying_to
            and thought.replying_to[-1].residence is residence
        ):
            self._discard_inbox_thought(thought, reason)

    def _discard_residence_batches(self, residence: InboxResidence, reason: str) -> None:
        queued = [heard for heard in self._unanswered if heard.residence is residence]
        if queued:
            target = queued[-1].perception
            self._timeline.append(
                EVENT_PLANNING_INTERRUPTED,
                reason,
                ActorType.ME,
                visibility=target.visibility,
                metadata={
                    "round_id": queued[0].record.id,
                    "in_reply_to": [heard.record.id for heard in queued],
                    "channel": target.channel,
                    "actions": [],
                },
            )
            self._unanswered.remove(queued)

    def _discard_inbox_thought(self, thought: _Thought, reason: str) -> None:
        if thought.inbox_discard_reason is None:
            self._invalidate_judgments(round_trace=thought.round_trace)
            self._thought = replace(thought, inbox_discard_reason=reason)
            thought.task.cancel()

    def _take_inbox_followups(self, inbox: InboxAttention) -> None:
        if self._halt_status.halted or self._stop_requested:
            return
        conversation = self._inbox_followup_key()
        arrivals, self._inbox_followups = self._inbox_followups, set()
        if inbox.active_conversation is not None:
            # D116：活跃会话不再提前返回，防抖窗口和规划期间的新未读并入当前批次。
            if conversation is not None and conversation in arrivals:
                self._submit_inbox(inbox.active_messages(now=self._clock.now()))
            self._take_in_perceptions(browsing=True, residence=inbox.residence)
        else:
            if conversation is not None and conversation in arrivals:
                self._submit_inbox(inbox.debounce_messages(conversation))
            self._take_in_perceptions(browsing=True)
        if (
            not self._config.planner_message_steering
            or conversation is None
            or conversation not in arrivals
            or conversation != self._thought_conversation()
        ):
            return
        count = self._planner_restarts.get(conversation, 0)
        if (
            count < self._config.planner_steering_max_interrupts
            and self._unanswered.peek_batch((None, conversation))
            and self._restart_planner()
        ):
            self._planner_restarts[conversation] = count + 1

    def _take_active_inbox(self, inbox: InboxAttention) -> None:
        conversation = inbox.active_conversation
        if (
            conversation is not None
            and conversation.platform in self._sinks
            and not self._stop_requested
            and self._permissions.check(ACTION_CALL_ORGAN).allowed
        ):
            # D116：思考或待回复批次进行中同样补读新到未读，交给 _maybe_start_thought 合入下一批。
            self._submit_inbox(inbox.active_messages(now=self._clock.now()))
            self._take_in_perceptions(browsing=True, residence=inbox.residence)

    def _restart_planner(self) -> bool:
        thought = self._thought
        if thought is None or thought.planner_run is None or thought.task.done():
            return False
        run = thought.planner_run
        if not run.is_planning or run.restart_requested:
            return False
        run.restart_requested = True
        self._invalidate_judgments(round_trace=thought.round_trace)
        thought.task.cancel()
        return True

    def _requeue_reply(self, thought: _Thought) -> None:
        for heard in thought.replying_to:
            person = (
                self._people.find(heard.perception.channel, heard.perception.sender_id)
                if self._people is not None
                else None
            )
            priority = self._reply_priority(heard.perception, person)
            if priority is not None:
                self._enqueue_reply(heard, priority)

    def _is_bound_user(self, perception: Perception) -> bool:
        if perception.route is None:
            return perception.trust is TrustLevel.VERIFIED_USER
        return self._platform_inbox is not None and self._platform_inbox.preempts(perception)

    def _reply_priority(self, perception: Perception, person: Person | None) -> Priority | None:
        """首次入队和规划重开共用同一份绑定与优先判断。"""
        inbox = self._platform_inbox
        if perception.route is None or inbox is None:
            return _priority_of(perception, person)
        if perception.trust is TrustLevel.UNVERIFIED:
            return None
        if inbox.preempts(perception):
            return Priority.USER
        if perception.trust is TrustLevel.VERIFIED_USER:
            return None
        return Priority.MENTION if inbox.is_priority(perception) else Priority.CONTACT

    def _clear_planner_restarts(self, conversation: ConversationKey | None) -> None:
        """会话失去阅读权限后忘掉它的重开计数，重新获准后从头计算（D138）。"""
        if conversation is not None:
            self._planner_restarts.pop(conversation, None)

    def _maybe_select_inbox(self, inbox: InboxAttention, *, had_thought: bool) -> None:
        reading = self._reading
        candidates = inbox.browse(
            can_browse=(
                not had_thought
                and self._thought is None
                and not self._unanswered
                and not self._pending
                and self.allows_emoji_delivery()
                and not self._residence_waiting_for_delivery()
                and (reading is None or not reading.sleep.night)
                and self._permissions.check(ACTION_CALL_ORGAN).allowed
            ),
        )
        if not candidates:
            return
        strategy = self._current_strategy()
        task = asyncio.create_task(
            select_conversations(
                candidates,
                self._call_organ,
                self._config.plan_organ,
                strategy.model_preference,
                read_limit=inbox.read_limit,
                stream=self._config.planner_message_steering,
            ),
            name=SELECTION_TASK,
        )
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task=task, replying_to=(), strategy=strategy)

    def _take_in_perceptions(
        self, *, browsing: bool = False, residence: InboxResidence | None = None
    ) -> None:
        """收感知：把新消息记进时间线。先记再出队，记不进去时消息还留在队里。"""
        inbox = self._platform_inbox
        received_for_residence = False
        while self._pending:
            perception, inbox_message = self._pending[0]
            message_residence = (
                residence
                if residence is not None
                and inbox_message is not None
                and inbox_message.conversation == residence.conversation
                else None
            )
            if inbox_message is not None and inbox is not None:
                previous = self._timeline.query(
                    TimelineQuery(
                        event_type=EVENT_MESSAGE,
                        metadata={"inbox_message_id": inbox_message.id},
                        limit=1,
                    )
                )
                if previous:
                    if message_residence is None:
                        inbox.store.mark_read([inbox_message.id])
                    self._pending.popleft()
                    continue
            person = self._identify(
                perception, "" if inbox_message is None else inbox_message.sender_name
            )
            metadata: dict[str, Any] = {
                "channel": perception.channel,
                "trust": perception.trust.value,
            }
            if person is not None:
                metadata["person_id"] = person.id
            if route := perception.route:
                metadata.update(
                    account_id=route.account_id,
                    recipient_id=route.recipient_id,
                    message_id=route.message_id,
                    message_type=route.kind,
                    mentioned=perception.mentioned,
                    quoted_message_id=perception.quoted_message_id,
                    source_timestamp=perception.source_timestamp,
                    temporary_group_id=route.temporary_group_id,
                )
            if perception.source_message_id:
                metadata["source_message_id"] = perception.source_message_id
            if any(component.kind == "reaction" for component in perception.components):
                metadata["qq_social_notice"] = "reaction"
            if inbox_message is not None:
                metadata.update(
                    inbox_message_id=inbox_message.id, sender_name=inbox_message.sender_name
                )
                if browsing:
                    metadata["inbox_browsed"] = True
                if person is not None and person.name:
                    metadata["person_name"] = person.name
            record = self._timeline.append(
                event_type=EVENT_MESSAGE,
                content=perception.content,
                actor_type=_actor_type_of(perception),
                actor_id=perception.sender_id,
                visibility=perception.visibility,
                metadata=metadata,
            )
            received_for_residence |= message_residence is not None
            self._pending.popleft()
            if inbox_message is not None and inbox is not None and message_residence is None:
                inbox.store.mark_read([inbox_message.id])
            priority = self._reply_priority(perception, person)
            if priority is None:
                # 确认不了身份（或通道和档案对不上）就不回，朝安全方向倒。
                logger.info(
                    "收到来自 %s 上未确认身份的账号的消息：只记录，不回复", perception.channel
                )
                continue
            heard = _Heard(
                perception,
                record,
                person is not None
                and person.is_user
                and perception.trust is TrustLevel.VERIFIED_USER,
                message_residence,
            )
            self._enqueue_reply(heard, priority)
            if priority is Priority.USER and perception.trust is TrustLevel.VERIFIED_USER:
                self._last_user_contact = perception
                self._initiative.note_user_message(record.timestamp)
        if received_for_residence and inbox is not None and inbox.residence is residence:
            inbox.renew(self._clock.now())

    def _enqueue_reply(self, heard: _Heard, priority: Priority) -> None:
        key = _platform_key(heard)
        inbox = self._platform_inbox
        # 防抖或快速反应都按平台会话攒批：重开时要能找到同一会话的待回复消息（D142）。
        group_by_route = key is not None and (
            heard.record.metadata.get("inbox_browsed") is True
            or self._config.message_debounce_mode != "off"
            or self._config.planner_message_steering
            or (inbox is not None and key == inbox.active_conversation)
        )
        self._unanswered.add(
            heard,
            priority,
            _reply_address(heard.perception, browsed=group_by_route),
            arrival=heard.record.id,
        )

    def _identify(self, perception: Perception, sender_name: str = "") -> Person | None:
        """按（通道, 账号）认人；没有人物档案时返回 None。

        sender_name 是来信显示的名字，只用来称呼，不参与认人（D130）。
        用户的账号只查不建：没登记的话，宁可认不出，也不把用户建成一个陌生的联系人。
        """
        if self._people is None:
            return None
        if perception.trust is TrustLevel.VERIFIED_USER:
            return self._people.find(perception.channel, perception.sender_id)
        return self._people.meet(perception.channel, perception.sender_id, sender_name)

    def _chat_round_current(
        self, trace: _RoundTrace, selected: _Heard, reply_id: int | None = None
    ) -> bool:
        chat = self._conversation.chat_plugins
        if chat is None:
            return not (trace.timed_chat or trace.owner_nudge)
        if trace.timed_chat and not chat.current(trace.timed_chat, selected.perception):
            return False
        nudge = trace.owner_nudge
        if not nudge or chat.nudge(nudge.target) == nudge:
            return True
        return bool(
            reply_id
            and chat.nudge_authorized(nudge)
            and self._timeline.query(
                TimelineQuery(
                    event_type=EVENT_REPLY_DELIVERED, metadata={"reply_id": reply_id}, limit=1
                )
            )
        )

    def _maybe_recheck_chat(self) -> None:
        chat = self._conversation.chat_plugins
        if (
            not chat
            or chat.remaining() > 0
            or self._thought
            or self._pending
            or self._unanswered
            or self._stop_requested
            or self._halt_status.halted
            or self._residence_waiting_for_delivery()
            or not self._permissions.check(ACTION_CALL_ORGAN).allowed
        ):
            return
        due = chat.take_due()
        if due is None:
            return
        item, anchor = due
        if item.target.platform not in self._sinks:
            return
        records = self._timeline.query(
            TimelineQuery(
                event_type=EVENT_MESSAGE, metadata={"inbox_message_id": anchor.id}, limit=1
            )
        )
        if not records:
            return
        heard = _Heard(
            anchor.perception, records[0], anchor.perception.trust is TrustLevel.VERIFIED_USER
        )
        attempt = self._timeline.append(
            "chat_recheck",
            "到期，仅重新判断原会话。",
            ActorType.SYSTEM,
            visibility=VisibilityScope.PRIVATE,
            metadata={"kind": item.kind, "anchor_id": records[0].id},
        )
        trace = _RoundTrace(attempt.id, timed_chat=item, used_platform_history=True)
        job = replace(self._reply_job((heard,)), round_trace=trace, media_sources=())
        task = asyncio.create_task(self._conversation._think(job), name="aureus-chat-recheck")
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task, (heard,), job.strategy, round_trace=trace)

    def _residence_waiting_for_delivery(self) -> bool:
        inbox = self._platform_inbox
        return (
            inbox is not None
            and inbox.residence is not None
            and self._reply_outbox.has_pending_conversation(inbox.residence.conversation)
        )

    def _expire_inbox_residence(self, inbox: InboxAttention) -> bool:
        residence = inbox.residence
        if (
            residence is None
            or self._thought is not None
            or self._residence_waiting_for_delivery()
            or not inbox.expire_if_due(self._clock.now())
        ):
            return False
        self._discard_residence_batches(residence, "会话驻留到期，未开始这批规划。")
        return True

    def _maybe_start_thought(self) -> None:
        """定焦点、想一想：有没回复的消息、又没在想别的，就在后台调用器官。"""
        if self._thought is not None or not self._unanswered or self._stop_requested:
            return
        permission = self._permissions.check(ACTION_CALL_ORGAN)
        if not permission.allowed:
            self._decline_unanswered(permission.reason)
            return
        if self._batch_wait_remaining() > 0:
            return  # 疲劳偏高：再等等，攒几条一起回（到点后下一拍会开始）
        inbox = self._platform_inbox
        active = inbox.active_conversation if inbox is not None else None
        if (
            inbox is not None
            and active is not None
            and not self._unanswered.peek_batch((None, active))
            and not self._residence_waiting_for_delivery()
        ):
            waiting = next(
                (
                    heard
                    for heard in self._unanswered
                    if _platform_key(heard) not in (None, active)
                    and inbox.yields_to_when_idle(heard.perception)
                ),
                None,
            )
            if waiting is not None:
                # 驻留会话空闲、也没有分段在打字：结束驻留，让别的会话的优先来信开始（D137、D138）。
                inbox.interrupt()
                active = None
        address = (None, active) if active is not None else None
        batch = sorted(self._unanswered.peek_batch(address), key=lambda heard: heard.record.id)
        if batch and _platform_key(batch[0]) is not None:
            batch = [
                heard
                for heard in batch
                if heard.perception.visibility is batch[0].perception.visibility
            ]
        if not batch or self._debounce_remaining(batch) > 0:
            return
        residence = batch[-1].residence
        if residence is not None and (inbox is None or not inbox.permits(residence)):
            self._clear_planner_restarts(residence.conversation)
            self._unanswered.remove(batch)
            return
        if inbox is not None and inbox.residence is not None and residence is None:
            if self._residence_waiting_for_delivery():
                # 旧驻留还有分段在打字：先让它发完，下一拍再切换，别丢掉正在投递的回复。
                return
            self._interrupt_inbox(inbox, "会话已切换，原批次未投递。")
        if (
            residence is None
            and self._working_memory is not None
            and self._config.working_memory_enabled
        ):
            # 最新 capacity 条进本轮上下文，更早的丢弃出上下文（D146）。
            # 丢弃只移出上下文，不删除时间线/收件箱原件，引用或 planner 查历史仍能取回。
            capacity = self._config.working_memory_capacity
            if len(batch) > capacity:
                self._unanswered.remove(batch[:-capacity])
                batch = batch[-capacity:]
        # 先组装材料再出队：组装出错时（比如读不了时间线），这些消息还留在队里。
        trace = _RoundTrace(
            batch[0].record.id,
            residence_deadline=(
                inbox.deadline if inbox is not None and residence is not None else None
            ),
        )
        job = replace(self._reply_job(batch), round_trace=trace)
        self._unanswered.remove(batch)
        key = _platform_key(batch[-1])
        if key is not None and (debouncer := self._debouncers.get(key)) is not None:
            # 批次已经取走：清掉这一批的起点和截止时间，保留上一条供下一批判断（D142）。
            debouncer.consume()
        run = None
        if self._config.planner_message_steering and key is not None:
            run = _PlannerRun()
            job = replace(job, planner_run=run)
        task = asyncio.create_task(
            self._conversation._think(job, self._register_judgment), name="aureus-thought"
        )
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(
            task=task,
            replying_to=batch,
            strategy=job.strategy,
            planner_run=run,
            round_trace=trace,
        )

    def _has_language_organ(self) -> bool:
        return (
            resolve_task_organ(
                self._language_config.organ, "language_learning", self._available_organs
            )
            in self._available_organs
        )

    def _language_pending(self) -> int:
        return sum(len(learner.pending) for learner in self._language_learners.values())

    def _collect_trimmed_learning(self, records: Sequence[TimelineRecord]) -> None:
        store = self._language
        if store is None:
            return
        for record in records:
            item = language_runner.trimmed_learning_sample(record)
            if item is None or item.conversation is None:
                continue
            scope = item.conversation
            if not store.learning_source_allowed(scope):
                continue
            for kind in language_runner.LanguageKind:
                key = (scope, kind)
                if key not in self._language_learners:
                    self._language_learners[key] = language_runner.TrimmedLearner(
                        kind, scope, PendingLearningBuffer(lambda sample: sample.source_key)
                    )
                self._language_learners[key].pending.extend((item.sample,))

    def _maybe_learn_language(self) -> None:
        """回复优先；同一来源有新样本时按上游间隔批量学习。"""
        if (
            self._language is None
            or not self._language_learners
            or self._thought is not None
            or self._unanswered
            or self._stop_requested
            or not self._permissions.check(ACTION_CALL_ORGAN).allowed
            or not self._has_language_organ()
        ):
            return
        now = self._clock.now()
        learner = next(
            (
                item
                for item in self._language_learners.values()
                if len(item.pending) >= language_runner.MIN_LEARNING_MESSAGES
                and (item.next_at is None or item.next_at <= now)
                and self._language.learning_source_allowed(item.scope)
            ),
            None,
        )
        if learner is None:
            self._language_last_status = "等待裁切素材或学习间隔"
            return
        batch = learner.batch()
        learner.next_at = now + language_runner.LEARNING_COOLDOWN
        self._language_last_status = "学习中"
        task = asyncio.create_task(
            language_runner.learn_language(
                self._language,
                self._language_config.organ,
                self._call_organ,
                batch,
                conversation_key=learner.scope,
                kinds=(learner.kind,),
                minimum=language_runner.MIN_LEARNING_MESSAGES,
                bot_names=self.bot_names(),
                expression_quality=(
                    language_runner.ExpressionQuality(
                        self._language_config.expression_quality_organ, self._clock
                    )
                    if self._language_config.expression_quality_enabled
                    else None
                ),
            ),
            name=language_runner.TASK_NAME,
        )
        task.add_done_callback(
            lambda finished: self._finish_language_task(finished, learner, batch)
        )
        self._thought = _Thought(task=task, replying_to=(), strategy=self._current_strategy())

    def _finish_language_task(
        self,
        task: asyncio.Task[bool],
        learner: language_runner.TrimmedLearner,
        batch: Sequence[language_runner.LanguageSample],
    ) -> None:
        try:
            error = task.exception()
        except asyncio.CancelledError:
            error = None
        if task.cancelled():
            self._language_last_status = "已取消，样本保留"
        elif error is not None:
            logger.error("语言学习任务失败（%s）；保留样本供重试。", type(error).__name__)
            self._language_last_status = "失败，样本保留"
        elif task.result() is True:
            learner.pending.discard(batch)
            self._language_last_status = "本批结束"
            self.live_events.publish(LiveEvent(TOPIC_RESOURCES, {"resource": RESOURCE_LANGUAGE}))
        else:
            self._language_last_status = "未完成，样本保留"
        self._on_thought_done(task)

    def _cancel_language_task(self) -> None:
        thought = self._thought
        if thought is not None and thought.task.get_name() == language_runner.TASK_NAME:
            thought.task.cancel()

    def _language_due_at(self) -> datetime | None:
        now = self._clock.now()
        return min(
            (
                item.next_at or now
                for item in self._language_learners.values()
                if len(item.pending) >= language_runner.MIN_LEARNING_MESSAGES
            ),
            default=None,
        )

    def _maybe_start_initiative(self) -> None:
        """只在这里读取主动需要的材料；失败就跳过这一拍（D78）。"""
        now = self._clock.now()
        tracker = self._initiative
        tracker.settle_timeout(now)
        if not tracker.can_consider or self._stop_requested:
            return
        if (
            self._platform_inbox is not None
            and self._platform_inbox.active_conversation is not None
        ):
            return
        if self._last_conversation_at is None and not self._restore_last_conversation():
            return
        try:
            target = self._last_user_contact
            person = (
                self._people.find(*_conversation_of(target))
                if target is not None and self._people is not None
                else None
            )
            person_id = person.id if person is not None else None
            items = (
                self._agenda.open_items(person_id)
                if self._agenda is not None and person_id is not None
                else []
            )
            due = [item for item in items if item.is_due(now)]
            if self._body is not None:
                self._reading = self._body.read(
                    len(self._unanswered) + len(self._pending), self._last_conversation_at
                )
            reading = self._reading
            social = reading.social_need if reading is not None else None
            mood = self._read_mood(person_id, viewer_is_user=True, strict=True)
            decision = decide(
                now=now,
                config=tracker.config,
                state=tracker.state,
                social_need=social,
                due_items=[f"#{item.id} {item.content}" for item in due],
                effective_tier=self._current_strategy().effective_tier,
                night=reading.sleep.night if reading is not None else False,
                halted=self._halt_status.halted,
                busy=self._thought is not None
                or bool(self._unanswered)
                or bool(self._pending)
                or (target is not None and self._reply_outbox.has_pending(target)),
                cautious=mood.cautious if mood is not None else False,
                has_channel=target is not None,
            )
            if decision.allowed and self._cortisol is not None and self._cortisol.should_defer(now):
                decision = InitiativeDecision(False, "皮质醇调节读数达到延后门槛", ())
            if not tracker.consider(decision) or target is None:
                return
            finding = self._exploration.finding_to_share(now)
            reasons = decision.reasons
            if finding is not None:
                reasons += (f"前几天查到：{finding.text}（来自网页《{finding.page_title}》）",)
            note = (
                f"【主动联系】这是你主动找{self._identity.user.name}，不是在回复。"
                f"原因：{'；'.join(reasons)}。被问起为什么找他，就照实说这些原因。"
            )
            agenda_state = (
                describe_agenda(items, now, self._timezone, limit=self._agenda.config.context_items)
                if self._agenda is not None
                else ""
            )
            job = _InitiativeJob(
                conversation=self._conversation_job(
                    target,
                    (),
                    person_id,
                    viewer_is_user=person is not None and person.is_user,
                    initiative_note=note,
                    mood_state=mood.summary if mood is not None else "",
                    agenda_state=agenda_state,
                ),
                reasons=reasons,
                social_need=social.level if social is not None else None,
                due_item_ids=tuple(item.id for item in due),
                finding_id=finding.id if finding is not None else None,
            )
            tracker.start(now)
            task = asyncio.create_task(self._initiate(job), name="aureus-initiative")
            task.add_done_callback(self._on_thought_done)
            self._thought = _Thought(task, (), job.conversation.strategy, initiative=job)
        except Exception:
            logger.exception("主动联系的材料读不了，这一拍不主动")

    def _maybe_start_exploration(self) -> None:
        """用户来信和主动联系之后才考虑探索，与它们共用一个任务槽（D79）。"""
        tracker, reading = self._exploration, self._reading
        if not tracker.config.enabled:
            return
        mood = self._read_mood(None, viewer_is_user=True)
        started = tracker.prepare(
            self._clock.now(),
            tier=self._current_strategy().effective_tier,
            night=reading.sleep.night if reading is not None else False,
            halted=self._halt_status.halted,
            busy=self._stop_requested
            or self._thought is not None
            or bool(self._unanswered)
            or bool(self._pending),
            cautious=mood.cautious if mood is not None else False,
        )
        if started is None or tracker.search is None:
            return
        task = asyncio.create_task(
            exploration_runner.explore(
                tracker.config,
                tracker.search,
                tracker.reader,
                self._call_organ,
                self._permissions,
                started.metadata["query"],
            ),
            name="aureus-exploration",
        )
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task, (), self._current_strategy(), exploration=started)

    def _maybe_start_diary(self) -> None:
        """其他任务之后才写日记，始终共用唯一任务槽（D80）。"""
        if self._diary is None:
            return
        job = self._diary.prepare(
            self._clock.now(),
            self._identity,
            tier=self._current_strategy().effective_tier,
            halted=self._halt_status.halted,
            busy=bool(self._stop_requested or self._thought or self._unanswered or self._pending),
        )
        if job is not None:
            task = asyncio.create_task(write_diary(job, self._call_organ), name="aureus-diary")
            task.add_done_callback(self._on_thought_done)
            self._thought = _Thought(task, (), self._current_strategy())

    def _maybe_start_persona_review(self) -> None:
        """日记收尾后的下一个空闲拍才回顾（D81），共用思考槽。"""
        tracker = self._persona_growth
        if tracker is None:
            return
        job = tracker.prepare(
            self._clock.now(),
            self._identity,
            diary_attempted_today=self._diary is not None
            and self._diary.attempted_day == tracker.calendar.day_of(self._clock.now()),
            tier=self._current_strategy().effective_tier,
            halted=self._halt_status.halted,
            busy=bool(self._stop_requested or self._thought or self._unanswered or self._pending),
        )
        if job is not None:
            task = asyncio.create_task(
                growth.review_persona(job, self._call_organ), name="aureus-persona-review"
            )
            task.add_done_callback(self._on_thought_done)
            self._thought = _Thought(task, (), self._current_strategy())

    def _maybe_summarize_working_memory(self) -> None:
        tracker = self._working_memory
        if (
            tracker is None
            or self._stop_requested
            or self._thought is not None
            or self._unanswered
            or self._pending
            or WORKING_MEMORY_ORGAN not in self._available_organs
            or not self._permissions.check(ACTION_CALL_ORGAN).allowed
        ):
            return
        strategy = self._current_strategy()
        if strategy.effective_tier is EnergyTier.OVERDRAFT:
            return
        batch = tracker.prepare(self._clock.now(), strategy.model_preference)
        if batch is not None:
            task = asyncio.create_task(summarize(batch, self._call_organ), name=WORKING_MEMORY_TASK)
            task.add_done_callback(self._on_thought_done)
            self._thought = _Thought(task, (), strategy)

    def _maybe_consolidate_memory(self) -> None:
        """夜间空闲时才检查积累量，沿用唯一后台思考槽。"""
        consolidator, reading = self._memory_consolidator, self._reading
        if (
            consolidator is None
            or reading is None
            or not reading.sleep.night
            or self._halt_status.halted
            or reading.energy.tier is EnergyTier.OVERDRAFT
            or self._stop_requested
            or self._thought
            or self._unanswered
            or self._pending
            or not consolidator.due(self._clock.now())
        ):
            return
        strategy = self._current_strategy()
        task = asyncio.create_task(
            consolidator.run(
                self._call_organ, strategy.model_preference, self._identity.facts.name
            ),
            name="aureus-memory-consolidation",
        )
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task, (), strategy)

    # ---------- 自动建索引（D125） ----------

    def _maybe_start_indexing(self) -> None:
        """有嵌入路线且没有在跑时，启动一个后台构建任务。"""
        if (
            self._indexing_task is not None
            or not self._indexing.auto_build_enabled
            or self._stop_requested
            or self._maintenance_paused
            or self._halt_status.halted
        ):
            return
        now = self._clock.now()
        if self._indexing_retry_at is not None and now < self._indexing_retry_at:
            return
        if not self._index_builders():
            return
        self._indexing_task = asyncio.create_task(
            self._build_indexes(), name="aureus-index-builder"
        )
        self._indexing_task.add_done_callback(self._indexing_finished)

    def _expression_scopes(self) -> frozenset[str]:
        """当前可见、已授权的表达来源范围；全局候选由索引自己并入。"""
        scopes: set[str] = set()
        if self._language is not None:
            pool = self._language.expression_pool()
            present = {scope for entry in pool for scope in entry.scope_keys}
            present.update(entry.restricted_scope for entry in pool if entry.restricted_scope)
            # 这里只挑两段的本机范围键；平台范围键由下面当前可见的会话给出。
            scopes.update(
                scope
                for scope in present
                if language_runner.allows_learning_scope(scope, lambda _conversation: False)
            )
        inbox = self._platform_inbox
        if inbox is not None:
            scopes.update(
                key.language_scope_key()
                for key in inbox.store.visible_conversation_keys()
                if inbox.allows(key)
            )
        return frozenset(scopes)

    def _index_builders(self) -> list[tuple[str, Callable[[], Awaitable[tuple[bool, bool]]]]]:
        """当前可自动构建的索引种类；每种返回（是否还有缺项，本批是否处理过）。"""
        builders: list[tuple[str, Callable[[], Awaitable[tuple[bool, bool]]]]] = []
        recall = self._conversation.expression_recall
        if recall is not None and recall.space is not None:
            builders.append((_INDEX_EXPRESSION, self._build_expression_batch))
        selector = self._conversation.emoji_selector
        if selector is not None and self._conversation.has_text_embedding and self._text_spaces:
            builders.append((_INDEX_EMOJI, self._build_emoji_batch))
        if self._image_backfill_available():
            builders.append((_INDEX_IMAGES, self._build_image_batch))
        return builders

    def _image_backfill_available(self) -> bool:
        return (
            self._inbox_image_index is not None
            and self._inbox_image_index.enabled
            and self._conversation.inbox_media.read is not None
            and self._platform_inbox is not None
        )

    async def _build_expression_batch(self) -> tuple[bool, bool]:
        recall = self._conversation.expression_recall
        if recall is None or recall.space is None:
            return False, False
        return await recall.build_authorized_batch(self._indexing.batch_size)

    async def _build_emoji_batch(self) -> tuple[bool, bool]:
        selector = self._conversation.emoji_selector
        if selector is None or not self._conversation.has_text_embedding:
            return False, False
        return await selector.prebuild_cache(self._text_spaces, self._indexing.batch_size)

    async def _build_image_batch(self) -> tuple[bool, bool]:
        images = self._inbox_image_index
        inbox = self._platform_inbox
        read = self._conversation.inbox_media.read
        if images is None or inbox is None or read is None or not images.enabled:
            return False, False
        return await images.backfill_batch(
            inbox.store, read, inbox.allows, self._indexing.batch_size
        )

    async def _build_indexes(self) -> None:
        """按批轮转补齐各索引；每批之后让出，条件不满足或全部完成时退出。

        返回的「是否还有缺项」为假时该种索引本轮完成。单批失败不抛出心跳：
        预算或精力拒绝暂停一段时间，其它失败记日志后下一轮再试（D125）。
        """
        completed: set[str] = set()
        stalled = 0
        try:
            while self._indexing_allowed():
                builders = self._index_builders()
                if not builders:
                    break
                progressed = False
                for kind, build in builders:
                    if kind in completed:
                        continue
                    more, did = await build()
                    if did:
                        resource = _RESOURCE_BY_INDEX.get(kind)
                        if resource is not None:
                            self.live_events.publish(
                                LiveEvent(TOPIC_RESOURCES, {"resource": resource})
                            )
                    if not more:
                        completed.add(kind)
                        continue
                    if did:
                        progressed = True
                        await asyncio.sleep(self._indexing.interval_seconds)
                        break
                if all(kind in completed for kind, _build in builders):
                    break
                if not progressed:
                    stalled += 1
                    if stalled >= _INDEX_BUILD_MAX_STALLED_ROUNDS:
                        logger.warning("索引构建连续 %s 轮没有进展，暂停到下一轮", stalled)
                        break
                    await asyncio.sleep(self._indexing.interval_seconds)
        except asyncio.CancelledError:
            raise
        except CallRejectedError as error:
            self._indexing_retry_at = self._clock.now() + timedelta(
                seconds=max(self._indexing.interval_seconds, _INDEXING_REJECTED_PAUSE_SECONDS)
            )
            logger.info("索引构建暂停（%s），条件恢复后继续", type(error).__name__)
        except (OrganCallError, ValueError, sqlite3.Error, OSError) as error:
            self._indexing_retry_at = self._clock.now() + timedelta(
                seconds=self._indexing.interval_seconds
            )
            logger.warning("索引构建这一轮未完成（%s），下一轮继续", type(error).__name__)
        except Exception:
            self._indexing_retry_at = self._clock.now() + timedelta(
                seconds=self._indexing.interval_seconds
            )
            logger.exception("索引构建出错，下一轮再试")
        else:
            self._indexing_retry_at = self._clock.now() + timedelta(
                seconds=self._indexing.interval_seconds
            )

    def _indexing_allowed(self) -> bool:
        """急停、关闭、维护暂停或总开关关闭时不再继续。"""
        return (
            self._indexing.auto_build_enabled
            and not self._stop_requested
            and not self._maintenance_paused
            and not self._halt_status.halted
        )

    def _cancel_indexing(self) -> None:
        """立即停下后台构建；已提交的批次保留。"""
        task = self._indexing_task
        if task is not None and not task.done():
            task.cancel()

    async def _wait_for_indexing(self) -> None:
        """关闭时等后台构建停下；取消不掉时只记日志，不拖住关闭。"""
        task = self._indexing_task
        self._indexing_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("后台索引构建异常结束")

    def _indexing_finished(self, task: asyncio.Task[None]) -> None:
        """构建任务结束后清掉引用；异常只记日志，不叫醒心跳。"""
        if self._indexing_task is task:
            self._indexing_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("后台索引构建异常结束（%s）", type(error).__name__)

    async def _initiate(self, job: _InitiativeJob) -> _ThoughtOutcome:
        """后台只问器官，不写状态；任何失败都朝不打扰的方向倒。"""
        conversation = job.conversation
        plan: InitiativePlan | None = None
        try:
            response = await self._call_organ(
                _organ_request(
                    conversation,
                    organ_id=self._config.plan_organ,
                    task="initiative_plan",
                    prompt=build_initiative_plan_prompt(
                        conversation.history, conversation.user_name, job.reasons, self._timezone
                    ),
                    system_context=conversation.full_context,
                    purpose=CallPurpose.OPTIONAL,
                )
            )
            plan = parse_initiative_plan(response.suggestion)
            if plan is None:
                return _ThoughtOutcome(notice="规划器返回的内容看不懂")
            if not plan.reach_out:
                return _ThoughtOutcome(initiative_plan=plan, declined=True)
            response = await self._call_organ(
                _organ_request(
                    conversation,
                    organ_id=self._config.reply_organ,
                    task="initiative_reply",
                    prompt=build_initiative_reply_prompt(
                        conversation.history,
                        conversation.user_name,
                        self._timezone,
                        plan.intent,
                        length=conversation.strategy.length_limit,
                        with_splits=self._config.split_reply_enabled,
                    ),
                    system_context=conversation.replyer_context,
                    purpose=CallPurpose.OPTIONAL,
                )
            )
            if not response.suggestion.strip():
                return _ThoughtOutcome(initiative_plan=plan, notice="回复器返回空内容")
            return await self._conversation._finish_reply(
                conversation, _ThoughtOutcome(initiative_plan=plan, response=response), response
            )
        except CallRejectedError as error:
            return _ThoughtOutcome(initiative_plan=plan, notice=error.reason)
        except Exception as error:
            logger.exception("主动联系调用器官失败，这次不说")
            return _ThoughtOutcome(
                initiative_plan=plan, notice=f"器官调用失败：{type(error).__name__}"
            )

    def _collect_initiative(self, thought: _Thought, outcome: _ThoughtOutcome) -> None:
        """核实当前权限和通道，再由 tracker 记录建议并调用发送。"""
        job = thought.initiative
        if job is None:
            return
        target = self._last_user_contact
        permission = self._permissions.check(ACTION_REPLY_TO_USER)
        if target is not None and target.channel not in self._sinks:
            target = None

        def send(
            destination: Perception,
            record: TimelineRecord,
            after_delivery: Callable[[], None],
            after_failure: Callable[[], None],
        ) -> None:
            self._deliver_reply(
                destination, record, outcome.reply_segments, after_delivery, after_failure
            )

        message_id = self._initiative.complete(
            {
                "reasons": list(job.reasons),
                "social_need": job.social_need,
                "due_item_ids": list(job.due_item_ids),
            },
            outcome.initiative_plan,
            outcome.response,
            target,
            outcome.reply_segments,
            send,
            outcome.notice or (permission.reason if not permission.allowed else ""),
        )
        if job.finding_id is not None and message_id is not None:
            self._exploration.mark_shared(job.finding_id, message_id, self._clock.now())

    def _decline_unanswered(self, reason: str) -> None:
        """不允许调用器官时（急停中）：没回复的消息已经在时间线里，这里只通知，之后也不补回复。

        每个对话对象只通知一次，不管他在这一拍里说了几句。
        """
        latest: dict[tuple[str, str], Perception] = {}
        for heard in self._unanswered.drain():
            latest[_conversation_of(heard.perception)] = heard.perception
        for target in latest.values():
            self._deliver(target, f"{reason}。这条消息已经记下，不会回复。", MessageKind.NOTICE)

    def _read_body(self) -> None:
        """读身体信号。账本读不了时沿用上一次的读数，不编一个新的。"""
        if self._body is None:
            return
        backlog = len(self._unanswered) + len(self._pending)
        try:
            self._reading = self._body.read(backlog, self._last_conversation_at)
        except Exception:
            # 为什么不抛：这里只是观察自己，读不到不该让心跳停下；真正的安全检查在网关里。
            logger.exception("读不了身体信号，沿用上一次的读数")

    def _record_body_sample(self) -> None:
        """按间隔把此刻的身体读数、焦点和收件箱条数记成一行样本（D64）。

        写失败只记日志、跳过这一行：样本丢一行只是曲线断一小段，不值得让心跳停下
        （和时间线不同，经历记不全才危险，见 D51）。失败后照样更新记样本的时刻，
        免得数据库出问题时每一拍都重试、刷屏日志。
        """
        reading = self._reading
        if self._samples is None or self._body is None or reading is None:
            return
        config = self._body.config
        if not config.sample_enabled:
            return
        now = self._clock.now()
        last = self._last_sample_at
        # 时钟往回调过时 elapsed 是负数：当作到点，免得曲线长时间不更新。
        if last is not None and 0 <= (now - last).total_seconds() < config.sample_interval_seconds:
            return
        self._last_sample_at = now
        focus = self._cognitive_state.attention_focus if self._cognitive_state else ""
        inbox_count = len(self._unanswered) + len(self._pending)
        try:
            self._samples.append(
                sample_from_reading(now, reading, focus=focus, inbox_count=inbox_count)
            )
        except Exception:
            logger.exception("身体信号样本没写进去，跳过这一行")

    def _check_stress(self) -> None:
        """压力刚变成偏高时做一次自检：记进时间线，并告诉用户（04 号文档第 5 节，D57）。"""
        reading = self._reading
        if reading is None or self._body is None:
            return
        stress = reading.stress
        rearm_below = self._body.config.stress_high_at * _STRESS_REARM_FRACTION
        if not stress.enabled or stress.level < rearm_below:
            self._stress_alarmed = False
            self._stress_lowering = False
        elif stress.high and self._body.config.stress_lowers_tier:
            self._stress_lowering = True
        if not self._body.config.stress_lowers_tier:
            self._stress_lowering = False
        if not stress.high or self._stress_alarmed:
            return
        self._stress_alarmed = True
        text = describe_self_check(reading)
        self._timeline.append(
            event_type=EVENT_SELF_CHECK,
            content=text,
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata={
                "stress": stress.level,
                "failed_calls": stress.failed_calls,
                "sampled_calls": stress.sampled_calls,
                "backlog": stress.backlog,
            },
        )
        logger.warning("压力偏高（%.0f%%），已自检", stress.level * 100)
        if self._last_user_contact is not None:
            self._deliver(self._last_user_contact, text, MessageKind.NOTICE)

    def _batch_wait_remaining(self) -> float:
        """疲劳偏高时，最早没回复的那条消息还要再等多少秒才开始回；0 表示现在就回。

        只拖延开始，不丢消息：到点后同一个人的几条消息一起回，少调用几次，让负荷降下来。
        """
        reading = self._reading
        if reading is None or self._body is None or not reading.fatigue.high:
            return 0.0
        if self._surprised():
            # 惊讶时这件事的注意力优先级上升：不等疲劳攒消息，马上处理（D69 第 3 条）。
            return 0.0
        oldest = self._unanswered.peek_oldest()
        if oldest is None:
            return 0.0
        batch_seconds = self._body.config.fatigue_batch_seconds
        waited = (self._clock.now() - oldest.record.timestamp).total_seconds()
        # 时钟往回调过时 waited 是负数：最多等 batch_seconds，不会无限等下去。
        return max(min(batch_seconds - waited, batch_seconds), 0.0)

    def _describe_agenda_counts(self) -> str:
        """/state 里的一行：未完成和已到期的约定各几条。"""
        if self._agenda is None:
            return ""
        try:
            items = self._agenda.open_items()
        except sqlite3.Error:
            return "日程读不了"
        now = self._clock.now()
        due = sum(1 for item in items if item.is_due(now))
        return f"日程：未完成 {len(items)} 条，其中已到期 {due} 条"

    def _hormone_levels(self) -> dict[str, float] | None:
        """此刻各激素的浓度，键是激素名；激素没开时是 None。"""
        hormones = self._hormones
        if hormones is None:
            return None
        now = self._clock.now()
        empathic = self._empathy_focus.active()
        return {
            kind.value: hormones.level(kind, now, extra=empathic)
            for kind in HormoneKind
            if kind_enabled(kind, hormones.config)
        }

    def _surprised(self) -> bool:
        """惊讶达到阈值了吗。"""
        hormones = self._hormones
        if hormones is None:
            return False
        level = hormones.level(
            HormoneKind.SURPRISE, self._clock.now(), extra=self._empathy_focus.active()
        )
        return level >= hormones.config.surprise_at

    def seconds_until_next_beat(self) -> float:
        """到下一个定时拍还有多少秒：夜里放慢；攒消息等到点的话，提前到点那一刻。"""
        interval = self._config.tick_interval_seconds
        reading = self._reading
        if reading is not None and reading.sleep.night and self._body is not None:
            interval = max(interval, self._body.config.night_tick_interval_seconds)
        if self._thought is None:
            inbox = self._platform_inbox
            active = inbox.active_conversation if inbox is not None else None
            if (
                inbox is not None
                and (deadline := inbox.deadline) is not None
                and not self._residence_waiting_for_delivery()
            ):
                interval = min(interval, max(0.0, (deadline - self._clock.now()).total_seconds()))
            if (
                not self._unanswered
                and not self._pending
                and not self._halt_status.halted
                and not self._stop_requested
                and not self._maintenance_paused
                and (chat := self._conversation.chat_plugins)
                and self._permissions.check(ACTION_CALL_ORGAN).allowed
            ):
                interval = min(interval, chat.remaining())
            address = (None, active) if active is not None else None
            remaining = max(
                self._batch_wait_remaining(),
                self._debounce_remaining(self._unanswered.peek_batch(address)),
            )
            if remaining > 0:
                interval = min(interval, remaining)
        return min(interval, self._reply_outbox.seconds_until_due())

    def _debounce_remaining(self, batch: Sequence[_Heard]) -> float:
        """这一批还要等多久才开始规划；关闭防抖、没有状态或已到期时是 0（D142）。"""
        key = _platform_key(batch[-1]) if batch else None
        if self._config.message_debounce_mode == "off" or key is None:
            return 0.0
        debouncer = self._debouncers.get(key)
        if debouncer is None or debouncer.deadline is None:
            return 0.0
        return max(0.0, debouncer.deadline - self._clock.now().timestamp())

    def _broadcast_cognitive_state(self) -> None:
        """广播认知状态：每拍都算；自述变了才写进时间线（D34、D41 第 4 条）。"""
        context: dict[str, Any] = {"task_name": TASK_IDLE}
        if self._thought is not None:
            context = {"task_name": TASK_REPLYING, "focus_target": self._identity.user.name}
        energy = self._reading.energy if self._reading is not None else None
        state = self._observer.observe(context, energy, self._hormone_levels())
        self._cognitive_state = state
        if not self._config.broadcast_cognitive_state:
            return
        statement = state.self_awareness_statement()
        if statement == self._last_broadcast:
            return
        self._timeline.append(
            event_type=EVENT_COGNITIVE_STATE,
            content=statement,
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata=state.to_dict(),
        )
        self._last_broadcast = statement

    # ---------- 网页实时事件（3.43） ----------

    def _update_live_pause(self) -> None:
        """急停、维护暂停或关闭时不推送网页实时事件；恢复后不补发暂停期间的变更。"""
        if self._stop_requested or self._maintenance_paused or self._halt_status.halted:
            self.live_events.pause()
        else:
            self.live_events.resume()

    def _publish_round_state(self) -> None:
        """正在回复谁的状态变了就推一条；暂停时先不记，恢复后的下一拍补上当前状态。"""
        state = self._round_state()
        if state == self._last_round_state or self.live_events.paused:
            return
        self._last_round_state = state
        active, web, conversation = state
        data: dict[str, object] = {"active": active}
        if active:
            data["web"] = web
            if conversation is not None:
                data["conversation"] = asdict(conversation)
        self.live_events.publish(LiveEvent(TOPIC_ROUND, data))

    def _round_state(self) -> tuple[bool, bool, ConversationKey | None]:
        """（是否正在回复，是否网页私聊，平台会话）；网页私聊的会话是 None。"""
        if self.is_replying_to(web_visibility=VisibilityScope.USER_ONLY):
            return (True, True, None)
        conversation = self._thought_conversation()
        if conversation is not None and self.is_replying_to(conversation=conversation):
            return (True, False, conversation)
        return (False, False, None)

    # ---------- 想法（后台任务） ----------

    def _current_strategy(self) -> ReplyStrategy:
        """按此刻的精力档位选这次回复的做法；没有身体读数时按正常档（D59）。

        压力偏高时降一档（D70）。
        """
        tier = self._reading.energy.tier if self._reading is not None else None
        return choose_strategy(tier, self._config, stress_high=self._stress_lowering)

    def _reply_job(self, batch: Sequence[_Heard]) -> _ReplyJob:
        changes = tuple(self._live_identity_changes)

        def build(*args: Any, **kwargs: Any) -> _ReplyJob:
            return self._conversation_job(*args, **kwargs, live_changes=changes)

        job = _reply_job(batch, build)
        self._live_identity_changes.clear()
        inbox = self._platform_inbox
        if job.residence is not None and inbox is not None:
            # D145 的已读水位与待答队列是两回事；已经上桌的提及也要纳入摘要。
            pending_ids = (
                message_id
                for heard in self._unanswered
                if type(message_id := heard.record.metadata.get("inbox_message_id")) is int
            )
            mentions = inbox.mention_pull_summary(
                job.residence.conversation, limit=None, pending_ids=pending_ids
            )
            job = with_mention_pull(job, mentions, self._clock.now())
        return job

    def _conversation_job(
        self,
        target: Perception,
        messages: Sequence[TimelineRecord],
        person_id: int | None,
        *,
        viewer_is_user: bool,
        initiative_note: str = "",
        mood_state: str | None = None,
        agenda_state: str | None = None,
        live_changes: Sequence[IdentityLayer] | None = None,
        frozen_history: _ReplyJob | None = None,
    ) -> _ReplyJob:
        return self._conversation._conversation_job(
            target,
            messages,
            person_id,
            viewer_is_user=viewer_is_user,
            initiative_note=initiative_note,
            mood_state=mood_state,
            agenda_state=agenda_state,
            frozen_history=frozen_history,
            strategy=self._current_strategy(),
            identity=self._identity,
            body=self._body,
            reading=self._reading,
            downtime=self._downtime_for_context(),
            identity_layers=self._identity_changes,
            live_identity_changes=self._live_identity_changes
            if live_changes is None
            else list(live_changes),
            interests=self._interests,
            findings=self._findings,
            affinity_for_context=lambda person_id: self._affinity_for_context(
                person_id, is_user=target.trust is TrustLevel.VERIFIED_USER
            ),
            read_mood=(
                self._prepare_mood(
                    person_id, viewer_is_user=target.trust is TrustLevel.VERIFIED_USER
                )
                if mood_state is None
                else None
            ),
            agenda_for_context=self._agenda_hooks._agenda_for_context,
            can_quote=bool(messages) and self._can_quote_reply(target),
            inbox_attention=self._inbox_attention_enabled(),
        )

    def _downtime_for_context(self) -> Downtime | None:
        """放进系统上下文的停机情况。睡眠信号关掉后不告诉它睡了多久（04 号文档第 7 节）。"""
        if self._config.roleplay_mode:
            return None  # 扮演模式没有睡眠信号（D65）
        if self._body is not None and not self._body.config.sleep_enabled:
            return None
        return self._downtime

    def _on_thought_done(self, _task: asyncio.Task[Any]) -> None:
        # 想法有结果了：立即跳一拍去处理，不等下一个定时拍。
        self._wake.set()

    # ---------- 其他 ----------

    async def _wait_for_next_beat(self) -> None:
        """等到有事（新感知、想法有结果、要求停下）或者到了定时拍。"""
        with contextlib.suppress(TimeoutError):
            timeout = None if self._maintenance_paused else self.seconds_until_next_beat()
            if timeout is not None and self._has_language_organ():
                due = self._language_due_at()
                now = self._clock.now()
                if due is not None and due > now:
                    timeout = min(timeout, (due - now).total_seconds())
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        # 先清掉再跳：清掉之后才来的事会重新叫醒；之前来的事，接下来这一拍都会处理。
        self._wake.clear()

    async def _settle_thought_before_shutdown(self) -> None:
        """关闭前：等进行中的想法最多 shutdown_grace_seconds 秒，等不到就取消（D41 第 6 条）。"""
        thought = self._thought
        if thought is None:
            return
        if not thought.task.done():
            logger.info("等进行中的器官调用结束，最多 %s 秒", self._config.shutdown_grace_seconds)
            await asyncio.wait({thought.task}, timeout=self._config.shutdown_grace_seconds)
        if not thought.task.done():
            thought.task.cancel()
            await asyncio.wait({thought.task}, timeout=_CANCEL_WAIT_SECONDS)
            if not thought.task.done():
                logger.warning("器官调用取消后 %s 秒还没停下，不再等它", _CANCEL_WAIT_SECONDS)
        if thought.task.done() and not thought.task.cancelled():
            self._collect_finished_thought()
            return
        self._thought = None
        self._record_abandoned(thought, halted=False)

    async def _settle_judgments_before_shutdown(self) -> None:
        """关闭前给后台判断一段时间，收下已完成的有效结果，再取消其余的（D41、D110）。"""
        running = {pending.task for pending in self._pending_judgments if not pending.task.done()}
        if running:
            await asyncio.wait(running, timeout=self._config.shutdown_grace_seconds)
        self._collect_finished_judgments()
        remaining = [pending for pending in self._pending_judgments if not pending.task.done()]
        for pending in remaining:
            pending.invalidated = True
            pending.task.cancel()
        if remaining:
            await asyncio.wait(
                {pending.task for pending in remaining}, timeout=_CANCEL_WAIT_SECONDS
            )
            if any(not pending.task.done() for pending in remaining):
                logger.warning("判断任务取消后 %s 秒还没停下，不再等它", _CANCEL_WAIT_SECONDS)
        self._pending_judgments.clear()

    def _record_abandoned(self, thought: _Thought, *, halted: bool) -> None:
        """记下被取消的想法：网关没机会记账，这次可能用掉的 token 不在账本里（0.8 已知缺口）。

        Args:
            thought: 被取消的想法。
            halted: 是因为急停取消的；否则是关闭时等不到。
        """
        self._invalidate_judgments(round_trace=thought.round_trace)
        if thought.inbox_discard_reason is not None:
            self._record_interrupted_round(thought, thought.inbox_discard_reason)
            return
        if thought.task.get_name() == SELECTION_TASK:
            return
        if thought.exploration is not None or thought.task.get_name() == "aureus-diary":
            return  # 取消探索或日记不写结果，也不补记（D79、D80）。
        if thought.task.get_name() in ("aureus-persona-review", language_runner.TASK_NAME):
            return  # D81：取消不写结果，重启后允许再试。
        if thought.task.get_name() in ("aureus-memory-consolidation", WORKING_MEMORY_TASK):
            return
        if thought.initiative is not None:
            self._collect_initiative(
                thought, _ThoughtOutcome(notice="急停时取消" if halted else "关闭时取消")
            )
            return
        cause = "急停时" if halted else "关闭时等不到进行中的器官调用，只好"
        self._record_interrupted_round(thought, "急停或关闭时取消，未投递。")
        self._timeline.append(
            event_type=EVENT_ORGAN_CALL_ABANDONED,
            content=f"{cause}取消了进行中的器官调用；这次可能已经用掉的 token 没有记进账本。",
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata={
                "organ_id": self._config.reply_organ,
                **self._round_metadata(thought),
            },
        )
        self._deliver(
            thought.replying_to[-1].perception,
            "急停中，这条消息已经记下，没有回复。"
            if halted
            else "正在关闭，这条消息来不及回复了。",
            MessageKind.NOTICE,
        )

    def _deliver(
        self,
        target: Perception,
        content: str,
        kind: MessageKind,
        record: TimelineRecord | None = None,
        *,
        emoji_digest: str | None = None,
        qq_action: QQChatAction | None = None,
        quote_source: bool = False,
        confirm_delivery: Callable[[], None] | None = None,
        confirm_platform_delivery: Callable[[str], None] | None = None,
        report_delivery_failure: Callable[[str], None] | None = None,
        reply_segment: int | None = None,
        can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        """把消息交给通道。通道出错只记日志：任何通道出问题都不影响核心（03 号文档）。"""
        sender = self._identity.facts.name if kind is MessageKind.REPLY else NOTICE_SENDER
        message = OutgoingMessage(
            channel=target.channel,
            recipient_id=target.sender_id,
            content=content,
            kind=kind,
            sender_name=sender,
            timeline_id=record.id if record is not None else None,
            timestamp=record.timestamp if record is not None else self._clock.now(),
            route=target.route,
            emoji_digest=emoji_digest,
            qq_action=qq_action,
            quote_source=quote_source,
            confirm_delivery=confirm_delivery,
            confirm_platform_delivery=confirm_platform_delivery,
            can_deliver=can_deliver,
            visibility=target.visibility,
            report_delivery_failure=report_delivery_failure,
            reply_segment=reply_segment,
        )
        sink = self._sinks.get(message.channel)
        if sink is None:
            logger.warning("通道 %s 没有接上，消息发不出去", message.channel)
            return False
        try:
            sink(message)
        except Exception:
            logger.exception("通道 %s 没能发出消息", message.channel)
            return False
        if confirm_delivery is not None and target.channel not in ("qq", "web"):
            confirm_delivery()
        return True


def _actor_type_of(perception: Perception) -> ActorType:
    """确认是用户本人的记成用户，其余记成某个联系人（具体是谁看 metadata 里的 person_id）。"""
    if perception.trust is TrustLevel.VERIFIED_USER:
        return ActorType.USER
    return ActorType.PERSON


def _conversation_of(perception: Perception) -> tuple[str, str]:
    """一个对话 = 一个通道里的一个账号；收件箱按它把同一个人的几句话合成一次回复。"""
    return (perception.channel, perception.sender_id)


def _reply_address(
    perception: Perception, *, browsed: bool = False
) -> tuple[tuple[str, str] | None, ConversationKey | None]:
    route = ConversationKey.from_perception(perception) if perception.route is not None else None
    return (None if browsed and route is not None else _conversation_of(perception)), route


def _platform_key(heard: _Heard) -> ConversationKey | None:
    if "inbox_message_id" in heard.record.metadata and heard.perception.route is not None:
        return ConversationKey.from_perception(heard.perception)
    return None


def _debounce_shape(perception: Perception) -> tuple[bool, bool]:
    """按消息形状返回（裸图, 纯表情）：没有文字才算（D142）。

    通道会给纯媒体消息写入「[图片]」「[表情]」这类兼容正文，所以有结构化段时只看 text 段；
    同时有图和表情按图片处理。
    """
    if perception.components:
        has_text = any(
            item.kind == "text" and (item.text or "").strip() for item in perception.components
        )
    else:
        has_text = bool(perception.content.strip())
    images = [item for item in perception.attachments if item.kind == "image"]
    if has_text or not images:
        return False, False
    is_bare_image = any(not item.is_emoji for item in images)
    is_pure_emoji = all(item.is_emoji for item in images)
    return is_bare_image, is_pure_emoji


def _priority_of(perception: Perception, person: Person | None) -> Priority | None:
    """这条消息按什么优先级排队；不该回复的返回 None（只记录）。

    通道说是别人、档案却说这个账号是用户，两边对不上：宁可不回，也不当成用户或别人处理。
    """
    if perception.trust is TrustLevel.VERIFIED_USER:
        return Priority.USER
    if perception.trust is TrustLevel.VERIFIED_CONTACT:
        if person is not None and person.is_user and perception.route is None:
            return None
        return Priority.CONTACT
    return None
