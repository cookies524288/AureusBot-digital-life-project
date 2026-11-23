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
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, tzinfo
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
    InitiativeConfig,
    JudgeConfig,
    LanguageConfig,
    MemoryConfig,
    PeopleConfig,
    PersonaGrowthConfig,
)
from aureus.common.config import CortisolConfig
from aureus.core import CognitiveObserver, CognitiveState
from aureus.feedback import FeedbackStore, Outcome
from aureus.heartbeat import exploration_runner, interest_changes, language_runner
from aureus.heartbeat import persona_growth_runner as growth
from aureus.heartbeat.agenda_hooks import AgendaHooks
from aureus.heartbeat.context import build_initiative_plan_prompt, build_initiative_reply_prompt
from aureus.heartbeat.conversation import (
    ConversationFlow,
    OrganCaller,
    _Heard,
    _InitiativeJob,
    _JudgeAttempt,
    _organ_request,
    _person_id_of,
    _PlannerRun,
    _reply_job,
    _ReplyJob,
    _Thought,
    _ThoughtOutcome,
)
from aureus.heartbeat.cortisol import CortisolTiming
from aureus.heartbeat.diary_runner import DiaryOutcome, DiaryTracker, write_diary
from aureus.heartbeat.downtime import Downtime, assess_downtime
from aureus.heartbeat.events import (
    EVENT_COGNITIVE_STATE,
    EVENT_EMERGENCY_RESUME,
    EVENT_EMERGENCY_STOP,
    EVENT_HORMONE_TRIGGERED,
    EVENT_IDENTITY_CHANGED,
    EVENT_JUDGMENT,
    EVENT_MESSAGE,
    EVENT_ORGAN_CALL_ABANDONED,
    EVENT_REPLY_DECLINED,
    EVENT_SELF_CHECK,
    EVENT_SHUT_DOWN,
    EVENT_WOKE_UP,
)
from aureus.heartbeat.inbox_attention import InboxAttention, InboxSelection, select_conversations
from aureus.heartbeat.inbox_media import AttachmentReader, ImageIndexer
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.heartbeat.initiative import InitiativeDecision, decide
from aureus.heartbeat.initiative_runner import InitiativeTracker, read_last_conversation
from aureus.heartbeat.judging import Judgment, TowardMeKind
from aureus.heartbeat.memory_consolidation import (
    ORGAN as MEMORY_CONSOLIDATION_ORGAN,
)
from aureus.heartbeat.memory_consolidation import ConsolidationOutcome, MemoryConsolidator
from aureus.heartbeat.perception import (
    MessageKind,
    MessageSink,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.planning import InitiativePlan, Plan, parse_initiative_plan
from aureus.heartbeat.recall import MemoryTracker
from aureus.heartbeat.reply_strategy import ReplyStrategy, choose_strategy
from aureus.heartbeat.self_check import describe_self_check
from aureus.heartbeat.speech import split_reply
from aureus.hormones import (
    HormoneEffects,
    HormoneKind,
    Hormones,
    HormoneTrigger,
    RetaliationMode,
    kind_enabled,
)
from aureus.identity import Identity, IdentityHistory, IdentityLayer
from aureus.memory import BATCH_SIZE, DiaryBook, LanguageSample, LanguageStore, MemoryIndex
from aureus.mood import FeedbackOutcome, Mood, MoodReading
from aureus.organs import CallPurpose, OrganRequest, OrganResponse
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
        memory_config: MemoryConfig | None = None,
        language: LanguageStore | None = None,
        language_config: LanguageConfig | None = None,
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
            memory_config: 记忆索引和召回的开关及上限（D80）。
            language: 统一的习得语言库（D89）。
            language_config: 语言召回、增量学习和参考字数配置。
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
        self._empathy = empathy
        self._mood = mood
        self._agenda = agenda
        self._feedback = feedback
        self._agenda_hooks = AgendaHooks(agenda, timeline, clock, feedback, timezone)
        self._language = language
        self._language_config = language_config or LanguageConfig()
        self._language_samples: deque[LanguageSample] = deque()
        # 模型热重载提供当前路线；清空备用器官的模型即可关闭其路由。
        self._available_organs: frozenset[str] = frozenset()
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
            agenda=agenda,
            memory=memory,
            memory_config=memory_config,
            language=language,
            language_config=self._language_config,
            people=people,
            people_config=people_config,
            working_memory=working_memory,
        )
        self._memory = MemoryTracker(memory, memory_config or MemoryConfig(), people, findings)
        self._memory_consolidator = (
            MemoryConsolidator(memory, self._memory.config)
            if memory is not None
            and self._memory.config.consolidation_enabled
            and not config.roleplay_mode
            else None
        )
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
        self._planner_restarts: dict[ConversationKey, int] = {}
        self._inbox_followups: set[ConversationKey] = set()
        self._wake = asyncio.Event()
        self._phase = _Phase.NOT_STARTED
        self._stop_requested = False
        self._maintenance_paused = False
        self._maintenance_task: asyncio.Task[Any] | None = None
        self._downtime: Downtime | None = None
        self._cognitive_state: CognitiveState | None = None
        self._last_broadcast: str | None = None

    # ---------- 给通道用的接口 ----------

    def update_available_organs(self, available: frozenset[str]) -> None:
        """模型热重载后同步可选总结任务的可用性，不改当天执行记录（D86）。"""
        self._available_organs = available
        self._conversation.has_text_embedding = "text_embedding" in available
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

    async def _call_organ(self, request: OrganRequest) -> OrganResponse:
        """在安全网关检查和记账之前选择实际承担该任务的器官。"""
        organ = resolve_task_organ(
            request.organ_id, request.context.get("task"), self._available_organs
        )
        if organ != request.organ_id:
            request = replace(request, organ_id=organ)
        return await self._organs.call(request)

    def attach_channel(self, name: str, sink: MessageSink) -> None:
        """接上一个通道：发往这个通道的消息交给 sink。

        Raises:
            ValueError: 同名的通道已经接上了。
        """
        if name in self._sinks:
            raise ValueError(f"通道 {name} 已经接上了")
        self._sinks[name] = sink

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
        if self._platform_inbox is not None:
            self._platform_inbox.allows = allows

    def set_inbox_attachment_reader(self, reader: AttachmentReader) -> None:
        """接上通道提供的后台收件箱附件读取器。"""
        self._conversation.inbox_media.read = reader

    def set_inbox_image_indexer(self, indexer: ImageIndexer) -> None:
        """把已经取回的图片交给同一收件箱的图像索引。"""
        self._conversation.inbox_media.index_image = indexer

    def on_inbox_message(self, message: InboxMessage) -> None:
        """来信已保存，重要来信立即叫醒心跳。"""
        inbox = self._platform_inbox
        if self._maintenance_paused or inbox is None:
            return
        if message.conversation == self._inbox_followup_key():
            self._inbox_followups.add(message.conversation)
            self._wake.set()
        elif inbox.is_priority(message.perception):
            self._wake.set()

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
            lines.append(f"回复做法：{self._current_strategy().describe()}")
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
        if self._thought is not None:
            lines.append(f"正在想怎么回复 {len(self._thought.replying_to)} 条消息。")
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

    @property
    def cognitive_state(self) -> CognitiveState | None:
        """最近一拍广播的认知状态；还没跳过时是 None。"""
        return self._cognitive_state

    @property
    def is_thinking(self) -> bool:
        """是否有进行中的想法（结果还没被处理）。"""
        return self._thought is not None

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
        if paused:
            self._inbox_followups.clear()
        if paused and self._thought is not None:
            thought = self._thought
            self._thought = None
            self._maintenance_task = thought.task
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
        had_thought = self._thought is not None
        diary_running = bool(self._thought and self._thought.task.get_name() == "aureus-diary")
        self._collect_finished_thought()
        self._memory.index_more()
        self._read_body()
        self._check_stress()
        self._agenda_hooks._check_agenda()
        if inbox is not None:
            self._maybe_select_inbox(inbox, had_thought=had_thought)
        thought = self._thought
        if (
            self._unanswered
            and thought is not None
            and thought.task.get_name() == WORKING_MEMORY_TASK
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
                self._take_in_perceptions()
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
            self._phase = _Phase.STOPPED
        logger.info("心跳已关闭：%s", reason)

    # ---------- 每一拍的步骤 ----------

    def _watch_emergency_stop(self) -> None:
        """读急停状态：变了就记进时间线；急停中就取消进行中的器官调用（D54）。"""
        status = self._emergency_stop.status()
        previous = self._halt_status
        self._halt_status = status
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
        if status.halted and thought is not None and not thought.task.done():
            # 取消的结果由 _collect_finished_thought 在下一拍处理（取消完成时会叫醒心跳）。
            thought.task.cancel()

    def _collect_finished_thought(self) -> None:
        """对结果：想法有结果了，把它说的话记进时间线、发回通道；失败就发系统通知。"""
        thought = self._thought
        if thought is None or not thought.task.done():
            return
        self._thought = None
        if thought.planner_run is not None and thought.planner_run.restart_requested:
            if not self._halt_status.halted:
                self._requeue_reply(thought)
            if not thought.task.cancelled():
                thought.task.exception()
            return
        if (conversation := self._thought_conversation(thought)) is not None:
            self._planner_restarts.pop(conversation, None)
        if thought.task.cancelled():
            self._record_abandoned(thought, halted=self._halt_status.halted)
            return
        outcome = thought.task.result()
        if thought.task.get_name() == language_runner.TASK_NAME:
            return
        assert outcome is not None
        if isinstance(outcome, InboxSelection):
            if self._platform_inbox is not None and not self._halt_status.halted:
                self._submit_inbox(self._platform_inbox.read_selection(outcome))
                self._take_in_perceptions(browsing=True)
            return
        if isinstance(outcome, SummaryOutcome):
            assert self._working_memory is not None
            self._working_memory.finish(outcome, self._clock.now())
            return
        if isinstance(outcome, ConsolidationOutcome):
            assert self._memory_consolidator is not None
            self._memory_consolidator.finish(outcome, self._clock.now())
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
        target = thought.replying_to[-1].perception
        person_id = _person_id_of(thought.replying_to[-1].record)
        if (
            self._platform_inbox is not None
            and outcome.plan is not None
            and not self._halt_status.halted
        ):
            plan = outcome.plan
            queued = self._unanswered.peek_batch()
            target_key = self._thought_conversation(thought)
            if target_key is not None and any(
                _platform_key(heard) not in (None, target_key)
                and self._platform_inbox.is_priority(heard.perception)
                for heard in queued
            ):
                plan = replace(plan, stay_in_conversation=False)
            self._platform_inbox.apply_plan(plan, target, thought.replying_to[-1].record)
        if outcome.judge is not None:
            judgment_record = self._record_judgment(thought, outcome.judge)
            self._record_triggers(outcome.judge.triggers)
            self._record_affinity(person_id, outcome.judge.judgment, judgment_record)
        if outcome.declined:
            if outcome.plan is not None:
                if self._hormones is not None:
                    self._hormones.note_decline(person_id, self._clock.now())
                self._record_declined(thought, outcome.plan, target, outcome.effects)
            return
        if outcome.response is None:
            self._deliver(target, outcome.notice, MessageKind.NOTICE)
            return
        reply_permission = self._permissions.check(ACTION_REPLY_TO_USER)
        if not reply_permission.allowed:
            # 器官已经返回了，但此刻不允许说话：这句话不进时间线（它没有说出口）。
            logger.warning("回复被权限检查拦下：%s", reply_permission.reason)
            self._deliver(
                target,
                f"{reply_permission.reason}。这条消息已经记下，没有回复。",
                MessageKind.NOTICE,
            )
            return
        text = outcome.response.suggestion.strip()
        metadata: dict[str, Any] = {
            "channel": target.channel,
            "in_reply_to": [heard.record.id for heard in thought.replying_to],
            "organ_id": outcome.response.organ_id,
            "strategy": thought.strategy.to_metadata(),
        }
        if outcome.plan is not None:
            metadata["plan"] = outcome.plan.to_metadata()
        if outcome.recall_keywords:
            metadata["recall_keywords"] = list(outcome.recall_keywords)
            metadata["recall_record_ids"] = list(outcome.recall_record_ids)
        if outcome.recall_source_keys:
            metadata["recall_source_keys"] = list(outcome.recall_source_keys)
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
        delivered = self._deliver_reply(target, reply_record)
        if delivered and target.trust is TrustLevel.VERIFIED_USER:
            self._last_conversation_at = self._clock.now()
        if delivered and outcome.plan is not None and person_id is not None:
            # 说出口了才算答应（D74 第 3 条）：规划器决定不回、或者没说出口时不记。
            self._agenda_hooks._apply_agenda(outcome.plan, person_id, reply_record)
            if outcome.plan.interest is not None and thought.replying_to[-1].is_user:
                interest_changes.apply_interest(
                    self._interests,
                    self._timeline,
                    self._exploration.calendar,
                    outcome.plan.interest,
                    thought.replying_to[-1].record,
                    self._clock.now(),
                )

    def _deliver_reply(self, target: Perception, record: TimelineRecord) -> bool:
        """把回复拆成几条短消息发出（D60）。

        时间线里仍然是完整的一条：它说的是一段话，拆开只是发出去的样子。
        关掉拆分、或者拆不出东西时，整段一条发出。
        """
        text = record.content
        parts: list[str] = []
        if self._config.split_reply_enabled:
            parts = split_reply(
                text,
                max_messages=self._config.max_reply_messages,
                min_chars=self._config.min_message_chars,
            )
        delivered = False
        for part in parts or [text]:
            delivered = self._deliver(target, part, MessageKind.REPLY, record) or delivered
        return delivered

    def _record_judgment(self, thought: _Thought, attempt: _JudgeAttempt) -> TimelineRecord:
        """把判断器官的结果记进时间线，只有自己可见（D68 第 7、8 条）。"""
        metadata: dict[str, Any] = {
            "in_reply_to": [heard.record.id for heard in thought.replying_to]
        }
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
        """激素触发写进状态和时间线（只有自己可见）；重启时靠这些记录还原浓度（D69 第 4、5 条）。"""
        if self._hormones is None or not triggers:
            return
        for trigger in triggers:
            self._timeline.append(
                event_type=EVENT_HORMONE_TRIGGERED,
                content=f"{trigger.kind.value} 上涨 {trigger.rise:.2f}：{trigger.reason}",
                actor_type=ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata=trigger.to_metadata(),
            )
        self._hormones.add(triggers, self._clock.now())
        if self._mood is not None:
            self._mood.add_triggers(triggers, self._clock.now())

    def _read_mood(
        self, person_id: int | None, *, viewer_is_user: bool, strict: bool = False
    ) -> MoodReading | None:
        """此刻的心情（D73）：激素贡献在内存里，反馈结果从反馈表读，再减去此刻压力。

        反馈表读不了时当作没有结果，不让心情这一步拖垮回复。
        """
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
        return mood.read(
            now,
            outcomes,
            stress_level=stress,
            person_id=person_id,
            viewer_is_user=viewer_is_user,
        )

    def _record_declined(
        self,
        thought: _Thought,
        plan: Plan,
        target: Perception,
        effects: HormoneEffects | None,
    ) -> None:
        """规划器决定不回复：记进时间线，并在终端提示一行，让用户知道不是出了故障（D59）。

        为什么要提示：用户看到的是「没有回应」，分不清是没看到、出了错、还是它选择不说话。
        """
        reason = plan.reason or "觉得不需要回应"
        metadata: dict[str, Any] = {
            "in_reply_to": [heard.record.id for heard in thought.replying_to],
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
        self._deliver(target, f"（没有回复：{reason}）", MessageKind.NOTICE)

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
        if self._config.inbox_debounce_enabled:
            batch = self._unanswered.peek_batch()
            return self._thought_conversation() or (_platform_key(batch[-1]) if batch else None)
        return None

    def _take_inbox_priority(self, inbox: InboxAttention) -> None:
        messages = inbox.priority_messages()
        active = inbox.active_conversation
        if active is not None and any(message.conversation != active for message in messages):
            inbox.active_conversation = None
            if self._thought_conversation() == active:
                self._restart_planner()
        self._submit_inbox(messages)

    def _take_inbox_followups(self, inbox: InboxAttention) -> None:
        if self._halt_status.halted or self._stop_requested:
            return
        conversation = self._inbox_followup_key()
        arrivals, self._inbox_followups = self._inbox_followups, set()
        if inbox.active_conversation is not None:
            self._submit_inbox(inbox.active_messages())
        elif conversation is not None and conversation in arrivals:
            self._submit_inbox(inbox.read_selection(InboxSelection((conversation,))))
        self._take_in_perceptions(browsing=True)
        if (
            not self._config.inbox_debounce_enabled
            or conversation is None
            or conversation not in arrivals
            or conversation != self._thought_conversation()
        ):
            return
        count = self._planner_restarts.get(conversation, 0)
        if (
            count < self._config.inbox_debounce_max_restarts
            and self._unanswered.peek_batch((None, conversation))
            and self._restart_planner()
        ):
            self._planner_restarts[conversation] = count + 1

    def _restart_planner(self) -> bool:
        thought = self._thought
        if thought is None or thought.planner_run is None or thought.task.done():
            return False
        run = thought.planner_run
        if not run.is_planning or run.restart_requested:
            return False
        run.restart_requested = True
        thought.task.cancel()
        return True

    def _requeue_reply(self, thought: _Thought) -> None:
        for heard in thought.replying_to:
            priority = Priority.USER if heard.is_user else Priority.CONTACT
            if (
                priority == Priority.CONTACT
                and _platform_key(heard) is not None
                and self._platform_inbox is not None
                and self._platform_inbox.is_priority(heard.perception)
            ):
                priority = Priority.SPECIAL
            self._enqueue_reply(heard, priority)

    def _maybe_select_inbox(self, inbox: InboxAttention, *, had_thought: bool) -> None:
        reading = self._reading
        candidates = inbox.browse(
            self._clock.now(),
            can_browse=(
                not had_thought
                and self._thought is None
                and not self._unanswered
                and not self._pending
                and not self._stop_requested
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
                stream=self._config.inbox_debounce_enabled,
            ),
            name="aureus-inbox-selection",
        )
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task=task, replying_to=(), strategy=strategy)

    def _take_in_perceptions(self, *, browsing: bool = False) -> None:
        """收感知：把新消息记进时间线。先记再出队，记不进去时消息还留在队里。"""
        while self._pending:
            perception, inbox_message = self._pending[0]
            inbox = self._platform_inbox
            if inbox_message is not None and inbox is not None:
                previous = self._timeline.query(
                    TimelineQuery(
                        event_type=EVENT_MESSAGE,
                        metadata={"inbox_message_id": inbox_message.id},
                        limit=1,
                    )
                )
                if previous:
                    inbox.store.mark_read([inbox_message.id])
                    self._pending.popleft()
                    continue
            person = self._identify(perception)
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
            self._pending.popleft()
            if inbox_message is not None and inbox is not None:
                inbox.store.mark_read([inbox_message.id])
            if self._language is not None and self._language_config.learning_enabled:
                self._language_samples.append(
                    language_runner.language_sample(record, perception.source_message_id)
                )
            priority = _priority_of(perception, person)
            if (
                priority is Priority.CONTACT
                and inbox_message is not None
                and inbox is not None
                and inbox.is_priority(perception)
            ):
                priority = Priority.SPECIAL
            if priority is None:
                # 确认不了身份（或通道和档案对不上）就不回，朝安全方向倒。
                logger.info(
                    "收到来自 %s 上未确认身份的账号的消息：只记录，不回复", perception.channel
                )
                continue
            heard = _Heard(
                perception,
                record,
                person is not None and person.is_user and priority is Priority.USER,
            )
            self._enqueue_reply(heard, priority)
            if priority is Priority.USER:
                self._last_user_contact = perception
                self._initiative.note_user_message(record.timestamp)

    def _enqueue_reply(self, heard: _Heard, priority: Priority) -> None:
        key = _platform_key(heard)
        inbox = self._platform_inbox
        group_by_route = key is not None and (
            heard.record.metadata.get("inbox_browsed") is True
            or self._config.inbox_debounce_enabled
            or (inbox is not None and key == inbox.active_conversation)
        )
        self._unanswered.add(
            heard, priority, _reply_address(heard.perception, browsed=group_by_route)
        )

    def _identify(self, perception: Perception) -> Person | None:
        """按（通道, 账号）认人；没有人物档案时返回 None。

        用户的账号只查不建：没登记的话，宁可认不出，也不把用户建成一个陌生的联系人。
        """
        if self._people is None:
            return None
        if perception.trust is TrustLevel.VERIFIED_USER:
            return self._people.find(perception.channel, perception.sender_id)
        return self._people.meet(perception.channel, perception.sender_id)

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
        active = self._platform_inbox.active_conversation if self._platform_inbox else None
        address = (None, active) if active is not None else None
        batch = sorted(self._unanswered.peek_batch(address), key=lambda heard: heard.record.id)
        if not batch or self._debounce_remaining(batch) > 0:
            return
        if self._working_memory is not None and self._config.working_memory_enabled:
            batch = batch[:self._config.working_memory_capacity]
        # 先组装材料再出队：组装出错时（比如读不了时间线），这些消息还留在队里。
        job = self._reply_job(batch)
        self._unanswered.remove(batch)
        run = None
        if self._config.inbox_debounce_enabled and _platform_key(batch[-1]) is not None:
            run = _PlannerRun()
            job = replace(job, planner_run=run)
        task = asyncio.create_task(self._conversation._think(job), name="aureus-thought")
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(
            task=task, replying_to=batch, strategy=job.strategy, planner_run=run
        )

    def _maybe_learn_language(self) -> None:
        """回复优先。空闲时用同一个器官任务槽处理本次运行的新消息。"""
        if (
            self._language is None
            or not self._language_samples
            or self._thought is not None
            or self._unanswered
            or self._stop_requested
            or not self._permissions.check(ACTION_CALL_ORGAN).allowed
        ):
            return
        samples = [
            self._language_samples.popleft()
            for _ in range(min(BATCH_SIZE, len(self._language_samples)))
        ]
        task = asyncio.create_task(
            language_runner.learn_language(
                self._language, self._language_config.organ, self._call_organ, samples
            ),
            name=language_runner.TASK_NAME,
        )
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task=task, replying_to=(), strategy=self._current_strategy())

    def _maybe_start_initiative(self) -> None:
        """只在这里读取主动需要的材料；失败就跳过这一拍（D78）。"""
        now = self._clock.now()
        tracker = self._initiative
        tracker.settle_timeout(now)
        if not tracker.can_consider or self._stop_requested:
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
                busy=self._thought is not None or bool(self._unanswered) or bool(self._pending),
                cautious=self._mood.cautious if self._mood is not None else False,
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
        started = tracker.prepare(
            self._clock.now(),
            tier=self._current_strategy().effective_tier,
            night=reading.sleep.night if reading is not None else False,
            halted=self._halt_status.halted,
            busy=self._stop_requested
            or self._thought is not None
            or bool(self._unanswered)
            or bool(self._pending),
            cautious=self._mood.cautious if self._mood is not None else False,
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
            or resolve_task_organ(WORKING_MEMORY_ORGAN, WORKING_MEMORY_TASK, self._available_organs)
            not in self._available_organs
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
            consolidator.run(self._call_organ, strategy.model_preference),
            name="aureus-memory-consolidation",
        )
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task, (), strategy)

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
                    ),
                    system_context=conversation.replyer_context,
                    purpose=CallPurpose.OPTIONAL,
                )
            )
            if not response.suggestion.strip():
                return _ThoughtOutcome(initiative_plan=plan, notice="回复器返回空内容")
            return _ThoughtOutcome(initiative_plan=plan, response=response)
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
        message_id = self._initiative.complete(
            {
                "reasons": list(job.reasons),
                "social_need": job.social_need,
                "due_item_ids": list(job.due_item_ids),
            },
            outcome.initiative_plan,
            outcome.response,
            target,
            self._deliver_reply,
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
        return {
            kind.value: hormones.level(kind, now)
            for kind in HormoneKind
            if kind_enabled(kind, hormones.config)
        }

    def _surprised(self) -> bool:
        """惊讶达到阈值了吗。"""
        hormones = self._hormones
        if hormones is None:
            return False
        level = hormones.level(HormoneKind.SURPRISE, self._clock.now())
        return level >= hormones.config.surprise_at

    def seconds_until_next_beat(self) -> float:
        """到下一个定时拍还有多少秒：夜里放慢；攒消息等到点的话，提前到点那一刻。"""
        interval = self._config.tick_interval_seconds
        reading = self._reading
        if reading is not None and reading.sleep.night and self._body is not None:
            interval = max(interval, self._body.config.night_tick_interval_seconds)
        if self._thought is None:
            active = self._platform_inbox.active_conversation if self._platform_inbox else None
            address = (None, active) if active is not None else None
            remaining = max(
                self._batch_wait_remaining(),
                self._debounce_remaining(self._unanswered.peek_batch(address)),
            )
            if remaining > 0:
                interval = min(interval, remaining)
        return interval

    def _debounce_remaining(self, batch: Sequence[_Heard]) -> float:
        if not self._config.inbox_debounce_enabled or not batch or _platform_key(batch[-1]) is None:
            return 0.0
        latest = max(heard.record.timestamp for heard in batch)
        waited = max(0.0, (self._clock.now() - latest).total_seconds())
        return max(0.0, self._config.inbox_debounce_seconds - waited)

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

    # ---------- 想法（后台任务） ----------

    def _current_strategy(self) -> ReplyStrategy:
        """按此刻的精力档位选这次回复的做法；没有身体读数时按正常档（D59）。

        压力偏高时降一档（D70）。
        """
        tier = self._reading.energy.tier if self._reading is not None else None
        return choose_strategy(tier, self._config, stress_high=self._stress_lowering)

    def _reply_job(self, batch: Sequence[_Heard]) -> _ReplyJob:
        return _reply_job(batch, self._conversation_job)

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
    ) -> _ReplyJob:
        return self._conversation._conversation_job(
            target,
            messages,
            person_id,
            viewer_is_user=viewer_is_user,
            initiative_note=initiative_note,
            mood_state=mood_state,
            agenda_state=agenda_state,
            strategy=self._current_strategy(),
            identity=self._identity,
            body=self._body,
            reading=self._reading,
            downtime=self._downtime_for_context(),
            identity_layers=self._identity_changes,
            live_identity_changes=self._live_identity_changes,
            interests=self._interests,
            findings=self._findings,
            affinity_for_context=lambda person_id: self._affinity_for_context(
                person_id, is_user=target.trust is TrustLevel.VERIFIED_USER
            ),
            read_mood=lambda person_id: self._read_mood(
                person_id, viewer_is_user=target.trust is TrustLevel.VERIFIED_USER
            ),
            agenda_for_context=self._agenda_hooks._agenda_for_context,
            inbox_attention=(
                self._platform_inbox is not None
                and self._config.inbox_attention_enabled
                and not self._config.roleplay_mode
            ),
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

    def _record_abandoned(self, thought: _Thought, *, halted: bool) -> None:
        """记下被取消的想法：网关没机会记账，这次可能用掉的 token 不在账本里（0.8 已知缺口）。

        Args:
            thought: 被取消的想法。
            halted: 是因为急停取消的；否则是关闭时等不到。
        """
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
        self._timeline.append(
            event_type=EVENT_ORGAN_CALL_ABANDONED,
            content=f"{cause}取消了进行中的器官调用；这次可能已经用掉的 token 没有记进账本。",
            actor_type=ActorType.ME,
            visibility=VisibilityScope.PRIVATE,
            metadata={
                "organ_id": self._config.reply_organ,
                "in_reply_to": [heard.record.id for heard in thought.replying_to],
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
