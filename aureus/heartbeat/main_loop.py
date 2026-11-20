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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from enum import Enum, auto
from typing import Any, Protocol

from aureus.agenda import AgendaItem, AgendaStore, ItemStatus, describe_agenda, parse_due
from aureus.attention import Inbox, Priority
from aureus.body import Body, BodyReading, BodySampleStore, sample_from_reading
from aureus.common import (
    BudgetCalendar,
    Clock,
    EmpathyConfig,
    HeartbeatConfig,
    InitiativeConfig,
    JudgeConfig,
    PeopleConfig,
)
from aureus.core import CognitiveObserver, CognitiveState
from aureus.feedback import (
    COMMITMENT_KEPT,
    PRIOR_CONFIDENCE,
    PROACTIVE_CONTACT,
    FeedbackError,
    FeedbackStore,
    Outcome,
)
from aureus.heartbeat.body_summary import ROLEPLAY_NOTE, describe_body_state
from aureus.heartbeat.context import (
    build_initiative_plan_prompt,
    build_initiative_reply_prompt,
    build_judge_context,
    build_judge_prompt,
    build_plan_prompt,
    build_reply_prompt,
    build_system_context,
)
from aureus.heartbeat.downtime import Downtime, assess_downtime
from aureus.heartbeat.events import (
    EVENT_AGENDA_ADDED,
    EVENT_AGENDA_CLOSED,
    EVENT_AGENDA_DUE,
    EVENT_COGNITIVE_STATE,
    EVENT_EMERGENCY_RESUME,
    EVENT_EMERGENCY_STOP,
    EVENT_HORMONE_TRIGGERED,
    EVENT_IDENTITY_CHANGED,
    EVENT_INITIATIVE_CONSIDERED,
    EVENT_INITIATIVE_RESULT,
    EVENT_JUDGMENT,
    EVENT_MESSAGE,
    EVENT_ORGAN_CALL_ABANDONED,
    EVENT_REPLY_DECLINED,
    EVENT_SELF_CHECK,
    EVENT_SHUT_DOWN,
    EVENT_WOKE_UP,
)
from aureus.heartbeat.initiative import InitiativeState, decide
from aureus.heartbeat.initiative_history import read_initiative_history
from aureus.heartbeat.judging import SENDER_SUBJECT, Judgment, TowardMeKind, parse_judgment
from aureus.heartbeat.perception import (
    MessageKind,
    MessageSink,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.planning import (
    InitiativePlan,
    Plan,
    ReplyLength,
    ResolutionResult,
    parse_initiative_plan,
    parse_plan,
)
from aureus.heartbeat.reply_strategy import ReplyStrategy, choose_strategy, judge_allowed
from aureus.heartbeat.self_check import describe_self_check
from aureus.heartbeat.speech import split_reply
from aureus.hormones import (
    HormoneEffects,
    HormoneKind,
    Hormones,
    HormoneTrigger,
    RetaliationMode,
    kind_enabled,
    make_triggers_from_empathy,
    make_triggers_from_judgment,
)
from aureus.identity import Identity, IdentityHistory, IdentityLayer
from aureus.mood import FeedbackOutcome, Mood, MoodReading
from aureus.organs import CallPurpose, OrganCallError, OrganRequest, OrganResponse
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
# D76：只往回看最近这些条，避免醒来时扫描整条时间线。
_CONVERSATION_RESTORE_LIMIT = 50
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


class OrganCaller(Protocol):
    """心跳调用器官用的对象。核心组装时传入安全网关 SafetyGateway，不传器官总线（D39、D41）。"""

    async def call(self, request: OrganRequest) -> OrganResponse:
        """经过安全检查后调用器官。"""
        ...


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


def _person_id_of(record: TimelineRecord) -> int | None:
    """这条消息是哪个人发的（人物 ID）；认不出是谁时是 None。"""
    person_id = record.metadata.get("person_id")
    if isinstance(person_id, int) and not isinstance(person_id, bool):
        return person_id
    return None


class _Phase(Enum):
    NOT_STARTED = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()


@dataclass(frozen=True)
class _Heard:
    """已经记进时间线、还没回复的一条消息。"""

    perception: Perception
    record: TimelineRecord


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
    # 规划器想不回，但这场冲突里已经不回过一次，代码要求这次必须回（D69 第 3 条）。
    forced_reply: bool = False
    # 主动联系独立解析，不能拿普通回复的兜底来替它决定开口（D76）。
    initiative_plan: InitiativePlan | None = None


@dataclass(frozen=True)
class _InitiativeJob:
    """拍里准备的主动材料，后台只用这些快照调用器官（D76）。"""

    conversation: _ReplyJob
    reasons: tuple[str, ...]
    social_need: float | None
    due_item_ids: tuple[int, ...]


@dataclass(frozen=True)
class _Thought:
    """进行中的想法：后台任务，和它要回复的消息、这次的做法。"""

    task: asyncio.Task[_ThoughtOutcome]
    replying_to: Sequence[_Heard]
    strategy: ReplyStrategy
    # 非空表示主动想法；仍与回复共用唯一任务槽（D34、D76）。
    initiative: _InitiativeJob | None = None


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
        calendar: BudgetCalendar | None = None,
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
            identity_history: 身份的版本历史，要已经和 identity.toml 对过；醒来时据此记下用户改了
                哪几层设定（D62）。不给就不记。
            samples: 身体信号样本存储，要已经打开；心跳按 [body] sample_interval_seconds
                定时记一行读数，观察台画曲线靠它（D64）。不给就不记样本。
            feedback: 反馈存储，要已经打开；认知状态里的信心读它（D67）。不给就显示没有数据。
            judge: 判断器官的配置；不给或 enabled 为 False 就不调用判断（D68）。
            hormones: 激素的内存状态，醒来时从时间线还原；不给就没有激素（D69）。
            affinity: 好感账，要已经打开；判断过的对话据此记好感变化，好感写进上下文（D71）。
                不给就没有好感。
            empathy: 同理心配置；不给或 enabled 为 False 就不共情（D72）。共情需要激素。
            mood: 心情；激素触发和反馈结果累积成心情，写进上下文，低时谨慎（D73）。
                醒来时从时间线还原；不给就没有心情。心情的激素来源需要激素。
            agenda: 日程，要已经打开；规划器答应的事记进去，到期提醒、兑现记进反馈（D74）。
                不给就没有日程。
            initiative: 主动联系的开关和退避参数；不给时关闭（D76）。
            calendar: 每日次数的分界，与预算共用日历；不给时按 timezone 创建。
        """
        self._clock = clock
        self._timeline = timeline
        self._organs = organs
        self._identity = identity
        self._config = config
        self._permissions = permissions
        self._emergency_stop = emergency_stop
        self._halt_status = HaltStatus(halted=False)  # 最近一拍看到的急停状态
        self._timezone = timezone
        self._body = body
        self._people = people
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
        self._conversation_restored = False
        self._observer = CognitiveObserver(clock, feedback)
        self._judge = judge
        self._judge_missing_logged = False  # 没配判断器官的日志只记一次
        self._hormones = hormones
        self._affinity = affinity
        self._empathy = empathy
        self._mood = mood
        self._agenda = agenda
        self._feedback = feedback
        self._initiative = initiative or InitiativeConfig()
        if config.roleplay_mode:
            self._initiative = self._initiative.model_copy(update={"enabled": False})
        if self._initiative.enabled and not config.planning_enabled:
            logger.warning("主动联系要用规划器，已关闭主动联系")
            self._initiative = self._initiative.model_copy(update={"enabled": False})
        self._calendar = calendar or BudgetCalendar(timezone)
        self._initiative_state = InitiativeState()
        self._initiative_day = self._calendar.day_of(clock.now())
        self._initiative_considered_id: int | None = None
        self._initiative_feedback_id: int | None = None
        # 已落盘的发送材料及待补考虑记录；反馈或时间线暂时写不了时，不能丢掉原预期。
        self._initiative_message: TimelineRecord | None = None
        self._initiative_consideration: dict[str, Any] | None = None
        # 用户已开口但结果暂时没写进去时，保留当时的判定，重试不能把及时回应算成超时。
        self._initiative_user_answer: bool | None = None
        self._initiative_restored = False
        self._initiative_blocked_by = ""
        # 本拍有状态读写失败时，正常回复照旧兜底，主动联系则停止，免得拿旧读数打扰用户。
        self._initiative_io_failed = False
        self._sinks: dict[str, MessageSink] = {}
        self._pending: deque[Perception] = deque()  # 收到了、还没记进时间线
        self._unanswered: Inbox[_Heard] = Inbox()  # 记进时间线了、还没开始回复；用户先回（1.8）
        self._thought: _Thought | None = None
        self._wake = asyncio.Event()
        self._phase = _Phase.NOT_STARTED
        self._stop_requested = False
        self._downtime: Downtime | None = None
        self._cognitive_state: CognitiveState | None = None
        self._last_broadcast: str | None = None

    # ---------- 给通道用的接口 ----------

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
        if self._phase in (_Phase.STOPPING, _Phase.STOPPED):
            logger.info("心跳正在关闭，没有收下来自 %s 的消息", perception.channel)
            return False
        self._pending.append(perception)
        self._wake.set()
        return True

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
        self._restore_initiative()
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

    def _restore_last_conversation(self) -> None:
        """从最近的回复恢复有效对话；主动开口和没发成功的话都不算（D76）。"""
        try:
            records = self._timeline.query(
                TimelineQuery(
                    event_type=EVENT_MESSAGE,
                    actor_type=ActorType.ME,
                    latest=True,
                    limit=_CONVERSATION_RESTORE_LIMIT,
                )
            )
            self._last_conversation_at = None
            for record in reversed(records):
                if record.metadata.get("initiative") is True:
                    continue
                if "to_user" in record.metadata:
                    if record.metadata["to_user"] is True:
                        self._last_conversation_at = record.timestamp
                        break
                    continue
                # 老版本没有 to_user，只认它明确回复过的用户记录。
                reply_ids = record.metadata.get("in_reply_to", [])
                if not isinstance(reply_ids, list):
                    continue
                for record_id in reply_ids:
                    if not isinstance(record_id, int) or isinstance(record_id, bool):
                        continue
                    source = self._timeline.query(TimelineQuery(since_id=record_id - 1, limit=1))
                    if (
                        source
                        and source[0].id == record_id
                        and source[0].actor_type is ActorType.USER
                    ):
                        self._last_conversation_at = record.timestamp
                        break
                if self._last_conversation_at is not None:
                    break
            self._conversation_restored = True
        except Exception:
            self._conversation_restored = False
            logger.exception("上次有效对话读不了，不凭空当作刚聊过")

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
        self._initiative_io_failed = False
        self._watch_emergency_stop()
        # D76：先收下已到的来信，不能把发送前排队的消息算作发送后的回应。
        self._take_in_perceptions()
        self._collect_finished_thought()
        self._read_body()
        self._check_stress()
        self._check_agenda()
        self._maybe_start_thought()
        self._maybe_start_initiative()
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
        if thought.task.cancelled():
            self._record_abandoned(thought, halted=self._halt_status.halted)
            return
        outcome = thought.task.result()
        if thought.initiative is not None:
            self._collect_initiative(thought, outcome)
            return
        target = thought.replying_to[-1].perception
        person_id = _person_id_of(thought.replying_to[-1].record)
        if outcome.judge is not None:
            judgment_record = self._record_judgment(thought, outcome.judge)
            self._record_triggers(outcome.judge.triggers)
            self._record_affinity(person_id, outcome.judge.judgment, judgment_record)
        if outcome.declined and outcome.plan is not None:
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
        if outcome.effects is not None:
            metadata["hormones"] = outcome.effects.to_metadata()
            if outcome.forced_reply:
                metadata["forced_reply"] = True
            if self._hormones is not None:
                retaliated = outcome.effects.retaliation is RetaliationMode.ALLOWED
                self._hormones.note_reply(person_id, self._clock.now(), retaliated=retaliated)
        delivered = self._deliver_reply(target, text)
        if target.trust is TrustLevel.VERIFIED_USER:
            metadata["to_user"] = delivered
            if delivered:
                self._last_conversation_at = self._clock.now()
        reply_record = self._timeline.append(
            event_type=EVENT_MESSAGE,
            content=text,
            actor_type=ActorType.ME,
            visibility=target.visibility,
            metadata=metadata,
        )
        if delivered and outcome.plan is not None and person_id is not None:
            # 说出口了才算答应（D74 第 3 条）：规划器决定不回、或者没说出口时不记。
            self._apply_agenda(outcome.plan, person_id, reply_record)

    def _deliver_reply(self, target: Perception, text: str) -> bool:
        """把回复拆成几条短消息发出（D60）。

        时间线里仍然是完整的一条：它说的是一段话，拆开只是发出去的样子。
        关掉拆分、或者拆不出东西时，整段一条发出。
        """
        parts: list[str] = []
        if self._config.split_reply_enabled:
            parts = split_reply(
                text,
                max_messages=self._config.max_reply_messages,
                min_chars=self._config.min_message_chars,
            )
        delivered = False
        for part in parts or [text]:
            delivered = self._deliver(target, part, MessageKind.REPLY) or delivered
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
            self._initiative_io_failed = True
            logger.exception("日程读不了，这一拍不检查到期")

    def _agenda_for_context(self, person_id: int | None) -> str:
        """这个人名下未完成的约定；没有日程或认不出是谁时不放。"""
        agenda = self._agenda
        if agenda is None or person_id is None:
            return ""
        try:
            items = agenda.open_items(person_id)
        except sqlite3.Error:
            self._initiative_io_failed = True
            logger.exception("日程读不了，这次上下文里不放日程")
            return ""
        return describe_agenda(
            items, self._clock.now(), self._timezone, limit=agenda.config.context_items
        )

    def _read_mood(self, person_id: int | None, *, viewer_is_user: bool) -> MoodReading | None:
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
                self._initiative_io_failed = True
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

    def _take_in_perceptions(self) -> None:
        """收感知：把新消息记进时间线。先记再出队，记不进去时消息还留在队里。"""
        while self._pending:
            perception = self._pending[0]
            person = self._identify(perception)
            metadata: dict[str, Any] = {
                "channel": perception.channel,
                "trust": perception.trust.value,
            }
            if person is not None:
                metadata["person_id"] = person.id
            record = self._timeline.append(
                event_type=EVENT_MESSAGE,
                content=perception.content,
                actor_type=_actor_type_of(perception),
                actor_id=perception.sender_id,
                visibility=perception.visibility,
                metadata=metadata,
            )
            self._pending.popleft()
            priority = _priority_of(perception, person)
            if priority is None:
                # 确认不了身份（或通道和档案对不上）就不回，朝安全方向倒。
                logger.info(
                    "收到来自 %s 上未确认身份的账号的消息：只记录，不回复", perception.channel
                )
                continue
            self._unanswered.add(_Heard(perception, record), priority, _conversation_of(perception))
            if priority is Priority.USER:
                self._last_user_contact = perception
                self._note_initiative_answer()

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
        # 一次只回复同一个通道里同一个人的消息，回复才知道发给谁；先回用户，同级先来先回。
        batch = self._unanswered.peek_batch()
        # 先组装材料再出队：组装出错时（比如读不了时间线），这些消息还留在队里。
        job = self._reply_job(batch)
        self._unanswered.take_next()
        task = asyncio.create_task(self._think(job), name="aureus-thought")
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task=task, replying_to=batch, strategy=job.strategy)

    # ---------- 主动联系（D76） ----------

    def _restore_initiative(self) -> None:
        """恢复计数和等待；发送通道只认这次运行里用户的来信。"""
        try:
            history = read_initiative_history(
                self._timeline, self._calendar, self._clock.now(), self._initiative
            )
            self._initiative_state = history.state
            self._initiative_considered_id = history.considered_id
            self._initiative_feedback_id = history.feedback_id
            self._initiative_message = history.message
            pending = history.state.pending_since
            self._initiative_user_answer = (
                history.answer_at - pending < timedelta(hours=self._initiative.answer_window_hours)
                if history.answer_at is not None and pending is not None
                else None
            )
            if history.message is not None and history.considered_id is None:
                snapshot = history.message.metadata.get("initiative_consideration", {})
                self._initiative_consideration = {
                    **snapshot,
                    "reasons": history.message.metadata.get("reasons", []),
                    "message_id": history.message.id,
                    "sent": None,
                    "decided": "failed",
                    "failure": "发送结果未留存，保守计入限额并等待回应",
                    "feedback_id": history.feedback_id,
                }
            self._initiative_day = self._calendar.day_of(self._clock.now())
            self._initiative_restored = True
        except Exception:
            self._initiative_restored = False
            self._initiative_io_failed = True
            logger.exception("主动联系的历史读不了，这一拍不主动")

    def _roll_initiative_day(self, now: datetime) -> None:
        day = self._calendar.day_of(now)
        if day != self._initiative_day:
            self._initiative_state = replace(self._initiative_state, contacts_today=0)
            self._initiative_day = day

    def _record_initiative_considered(self, metadata: dict[str, Any]) -> bool:
        """补留考虑结果；按来源检查写入结果，追加成功后抛错也不能重复记一次考虑。"""
        try:
            message_id = metadata.get("message_id")
            existing = (
                self._timeline.query(
                    TimelineQuery(
                        event_type=EVENT_INITIATIVE_CONSIDERED,
                        metadata={"message_id": message_id},
                        limit=1,
                    )
                )
                if isinstance(message_id, int)
                else []
            )
            record = (
                existing[0]
                if existing
                else self._timeline.append(
                    event_type=EVENT_INITIATIVE_CONSIDERED,
                    content="考虑主动联系：" + "；".join(metadata["reasons"]),
                    actor_type=ActorType.ME,
                    visibility=VisibilityScope.PRIVATE,
                    metadata=metadata,
                )
            )
            if metadata.get("sent") is not False:
                self._initiative_considered_id = record.id
            self._initiative_consideration = None
            return True
        except Exception:
            self._initiative_consideration = metadata
            self._initiative_io_failed = True
            logger.exception("主动联系的考虑记录没写进去，这一拍不再主动")
            return False

    def _ensure_initiative_records(self) -> bool:
        """先找回或补记原预期，再补考虑记录；存储恢复后每次行动仍只有一份反馈。"""
        try:
            if (
                self._initiative_state.pending_since is not None
                and self._feedback is not None
                and self._initiative_feedback_id is None
            ):
                message = self._initiative_message
                if message is None:
                    raise FeedbackError("主动联系缺少来源消息，不能臆造反馈预期")
                existing = self._feedback.find_by_source(PROACTIVE_CONTACT, message.id)
                if existing is not None:
                    self._initiative_feedback_id = existing.id
                else:
                    expected = message.metadata.get("initiative_expected")
                    self._initiative_feedback_id = self._feedback.record_expectation(
                        PROACTIVE_CONTACT,
                        expected,
                        source_id=message.id,
                        expected_at=message.timestamp,
                    )
            if self._initiative_consideration is not None:
                metadata = dict(self._initiative_consideration)
                metadata["feedback_id"] = self._initiative_feedback_id
                return self._record_initiative_considered(metadata)
            return True
        except Exception:
            self._initiative_io_failed = True
            logger.exception("主动联系的原始记录还没补齐，这一拍不再主动")
            return False

    def _settle_initiative_timeout(self) -> bool:
        pending = self._initiative_state.pending_since
        if pending is None:
            return True
        if self._initiative_user_answer is not None:
            return self._finish_initiative(answered=self._initiative_user_answer)
        window = timedelta(hours=self._initiative.answer_window_hours)
        if self._clock.now() - pending >= window:
            return self._finish_initiative(answered=False)
        return True

    def _note_initiative_answer(self) -> None:
        """用户先开口就清退避；迟到的回应先如实结算窗口外的失败，再重置。"""
        pending = self._initiative_state.pending_since
        if pending is not None:
            window = timedelta(hours=self._initiative.answer_window_hours)
            if self._initiative_user_answer is None:
                self._initiative_user_answer = self._clock.now() - pending < window
            self._finish_initiative(answered=self._initiative_user_answer)
        self._initiative_state = replace(self._initiative_state, unanswered_streak=0)

    def _finish_initiative(self, *, answered: bool) -> bool:
        """先补反馈、再留结果；重试时检查已有结果，避免重复记账。"""
        if not self._ensure_initiative_records():
            return False
        try:
            if self._feedback is not None and self._initiative_feedback_id is not None:
                record = self._feedback.get(self._initiative_feedback_id)
                if record is not None and record.outcome is not Outcome.UNKNOWN:
                    answered = record.outcome is Outcome.SUCCESS
                else:
                    self._feedback.record_outcome(
                        self._initiative_feedback_id,
                        Outcome.SUCCESS if answered else Outcome.FAILURE,
                    )
            existing = self._timeline.query(
                TimelineQuery(
                    event_type=EVENT_INITIATIVE_RESULT,
                    metadata={"considered_id": self._initiative_considered_id},
                    limit=1,
                )
            )
            if not existing:
                self._timeline.append(
                    event_type=EVENT_INITIATIVE_RESULT,
                    content="主动联系后用户回应了。" if answered else "主动联系后一直没有回应。",
                    actor_type=ActorType.ME,
                    visibility=VisibilityScope.PRIVATE,
                    metadata={
                        "answered": answered,
                        "considered_id": self._initiative_considered_id,
                        "message_id": self._initiative_message.id
                        if self._initiative_message is not None
                        else None,
                    },
                )
        except Exception:
            self._initiative_io_failed = True
            logger.exception("主动联系的回应结果没记上，这一拍不主动")
            return False
        self._initiative_state = replace(
            self._initiative_state,
            pending_since=None,
            unanswered_streak=0
            if answered or self._initiative_user_answer is not None
            else self._initiative_state.unanswered_streak + 1,
        )
        self._initiative_considered_id = None
        self._initiative_feedback_id = None
        self._initiative_message = None
        self._initiative_user_answer = None
        return True

    def _maybe_start_initiative(self) -> None:
        """用户消息先处理完，闲下来且每道关都通过时才考虑主动联系。"""
        if not self._initiative_restored:
            self._restore_initiative()
        if (
            not self._initiative_restored
            or not self._ensure_initiative_records()
            or not self._settle_initiative_timeout()
        ):
            return
        now = self._clock.now()
        self._roll_initiative_day(now)
        if not self._initiative.enabled or self._stop_requested or self._initiative_io_failed:
            return
        if not self._conversation_restored:
            self._restore_last_conversation()
            if not self._conversation_restored:
                return
        try:
            target = self._last_user_contact
            person = (
                self._people.find(*_conversation_of(target))
                if target is not None and self._people is not None
                else None
            )
            person_id = person.id if person is not None else None
            due = (
                [item for item in self._agenda.open_items(person_id) if item.is_due(now)]
                if self._agenda is not None and person_id is not None
                else []
            )
            self._read_mood(person_id, viewer_is_user=True)
            reading = self._reading
            social = reading.social_need if reading is not None else None
            decision = decide(
                now=now,
                config=self._initiative,
                state=self._initiative_state,
                social_need=social,
                due_items=[f"#{item.id} {item.content}" for item in due],
                effective_tier=self._current_strategy().effective_tier,
                night=reading.sleep.night if reading is not None else False,
                halted=self._halt_status.halted,
                busy=self._thought is not None or bool(self._unanswered) or bool(self._pending),
                cautious=self._mood.cautious if self._mood is not None else False,
                has_channel=target is not None,
            )
            if decision.blocked_by != self._initiative_blocked_by:
                self._initiative_blocked_by = decision.blocked_by
                if decision.blocked_by:
                    logger.debug("这一拍不主动：%s", decision.blocked_by)
            if not decision.allowed or target is None or self._initiative_io_failed:
                return
            if self._feedback is not None:
                # 先确认反馈可读，免得问完两个器官才发现不能如实记预期。
                self._feedback.confidence(PROACTIVE_CONTACT)
            note = (
                f"【主动联系】这是你主动找{self._identity.user.name}，不是在回复。"
                f"原因：{'；'.join(decision.reasons)}。被问起为什么找他，就照实说这些原因。"
            )
            job = _InitiativeJob(
                conversation=self._conversation_job(target, (), person_id, initiative_note=note),
                reasons=decision.reasons,
                social_need=social.level if social is not None else None,
                due_item_ids=tuple(item.id for item in due),
            )
            if self._initiative_io_failed:
                return
            self._initiative_state = replace(self._initiative_state, last_considered_at=now)
            task = asyncio.create_task(self._initiate(job), name="aureus-initiative")
            task.add_done_callback(self._on_thought_done)
            self._thought = _Thought(task, (), job.conversation.strategy, initiative=job)
        except Exception:
            self._initiative_io_failed = True
            logger.exception("主动联系的材料读不了，这一拍不主动")

    async def _initiate(self, job: _InitiativeJob) -> _ThoughtOutcome:
        """后台只问器官，不写状态；任何失败都朝不打扰的方向倒。"""
        conversation = job.conversation
        plan: InitiativePlan | None = None
        try:
            response = await self._organs.call(
                self._organ_request(
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
            response = await self._organs.call(
                self._organ_request(
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

    def _send_initiative(
        self, job: _InitiativeJob, response: OrganResponse, consideration: dict[str, Any]
    ) -> tuple[bool, int | None, str]:
        """检查此刻权限和通道，发送后才计次数；记反馈失败也不能把已发说成没发。"""
        permission = self._permissions.check(ACTION_REPLY_TO_USER)
        if not permission.allowed:
            return False, None, permission.reason
        target = self._last_user_contact
        if target is None or target.channel not in self._sinks:
            return False, None, "通道不在了"
        if self._initiative_io_failed:
            return False, None, "本拍状态读写失败"
        expected = PRIOR_CONFIDENCE
        if self._feedback is not None:
            value = self._feedback.confidence(PROACTIVE_CONTACT).value
            expected = value if value is not None else PRIOR_CONFIDENCE
        text = response.suggestion.strip()
        message = self._timeline.append(
            event_type=EVENT_MESSAGE,
            content=text,
            actor_type=ActorType.ME,
            visibility=target.visibility,
            metadata={
                "initiative": True,
                "reasons": list(job.reasons),
                "to_user": True,
                "channel": target.channel,
                "organ_id": response.organ_id,
                "initiative_expected": expected,
                "initiative_consideration": dict(consideration),
            },
        )
        self._initiative_message = message
        if not self._deliver_reply(target, text):
            return False, None, "通道发送失败"
        now = self._clock.now()
        self._roll_initiative_day(now)
        self._initiative_state = replace(
            self._initiative_state,
            contacts_today=self._initiative_state.contacts_today + 1,
            pending_since=message.timestamp,
        )
        try:
            feedback_id = (
                self._feedback.record_expectation(
                    PROACTIVE_CONTACT, expected, source_id=message.id, expected_at=message.timestamp
                )
                if self._feedback is not None
                else None
            )
            return True, feedback_id, ""
        except Exception:
            self._initiative_io_failed = True
            logger.exception("主动联系已发出，但反馈预期没记上")
            return True, None, "反馈预期没记上"

    def _collect_initiative(self, thought: _Thought, outcome: _ThoughtOutcome) -> None:
        """先尝试发送，再记如实的考虑结果；失败不向用户发系统通知。"""
        job = thought.initiative
        assert job is not None
        plan = outcome.initiative_plan
        sent, feedback_id, failure = False, None, outcome.notice
        decided = "skip" if plan is not None and not plan.reach_out and not failure else "failed"
        metadata: dict[str, Any] = {
            "reasons": list(job.reasons),
            "social_need": job.social_need,
            "due_item_ids": list(job.due_item_ids),
            "plan": plan.to_metadata() if plan is not None else None,
        }
        if plan is not None and plan.reach_out and outcome.response is not None:
            try:
                sent, feedback_id, failure = self._send_initiative(job, outcome.response, metadata)
                decided = "reach_out" if sent else "failed"
            except Exception:
                self._initiative_io_failed = True
                logger.exception("主动联系没能发出，这一拍不再主动")
                failure = "主动联系读写状态失败"
        if sent:
            self._initiative_feedback_id = feedback_id
        metadata.update(
            decided=decided,
            sent=sent,
            failure=failure,
            feedback_id=feedback_id,
            message_id=self._initiative_message.id
            if self._initiative_message is not None
            else None,
        )
        if not sent:
            self._initiative_message = None
        self._record_initiative_considered(metadata)

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
            self._initiative_io_failed = True
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
            remaining = self._batch_wait_remaining()
            if remaining > 0:
                interval = min(interval, remaining)
        return interval

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
        """组装回复要用的材料：做法由精力档位定，人设和醒来信息放系统上下文，对话放提示词。"""
        return self._conversation_job(
            batch[0].perception, [heard.record for heard in batch], _person_id_of(batch[-1].record)
        )

    def _conversation_job(
        self,
        target: Perception,
        messages: Sequence[TimelineRecord],
        person_id: int | None,
        *,
        initiative_note: str = "",
    ) -> _ReplyJob:
        """回复和主动联系共用相同的身份、身体、激素、心情和日程背景（D76）。"""
        strategy = self._current_strategy()
        batch_ids = {record.id for record in messages}
        contact = target.trust is not TrustLevel.VERIFIED_USER
        contact_account = f"{target.channel}:{target.sender_id}" if contact else None
        # 回复用户时只带同一可见范围的对话。回复别人时不按范围过滤（D61，用户决定）：
        # 它的记忆是连续的一份，防套话靠系统上下文里的说明，由规划器和回复器各自判断。
        recent = self._timeline.query(
            TimelineQuery(
                event_type=EVENT_MESSAGE,
                visibility=None if contact else target.visibility,
                latest=True,
                limit=strategy.message_limit,
            )
        )
        # 按序号排除这次要回复的消息，而不是只取“比它们早的”：想上一件事的时候来的消息，
        # 序号比上一次的回复还小，只取更早的就会漏掉它刚说过的话。
        history = [record for record in recent if record.id not in batch_ids]
        body_state = self._body_state_for_context(strategy)
        now = self._clock.now()
        downtime = self._downtime_for_context()
        identity_changes = (
            [layer.describe() for layer in self._identity_changes]
            if self._config.identity_changes_in_prompt
            else []
        )
        live_changes = (
            [layer.describe() for layer in self._live_identity_changes]
            if self._config.identity_changes_in_prompt
            else []
        )
        self._live_identity_changes = []  # 只在刚改完的这一次回复里说

        affinity_state = self._affinity_for_context(person_id, is_user=not contact)
        mood_reading = self._read_mood(person_id, viewer_is_user=not contact)
        mood_state = mood_reading.summary if mood_reading is not None else ""
        agenda_state = self._agenda_for_context(person_id)

        def context(*, include_behavior_style: bool, hormone_state: str = "") -> str:
            return build_system_context(
                self._identity,
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
                initiative_note=initiative_note if not include_behavior_style else "",
            )

        def build_contexts(full_text: str, replyer_text: str) -> tuple[str, str]:
            return (
                context(include_behavior_style=True, hormone_state=full_text),
                context(include_behavior_style=False, hormone_state=replyer_text),
            )

        judge_count = self._judge.context_messages if self._judge is not None else 0
        judge_history = list(history)[-judge_count:] if judge_count > 0 else []
        job = _ReplyJob(
            strategy=strategy,
            history=history,
            messages=messages,
            user_name=self._identity.user.name,
            full_context=context(include_behavior_style=True),
            replyer_context=context(include_behavior_style=False),
            judge_context=build_judge_context(self._identity),
            judge_history=judge_history,
            base_strategy=strategy,
            person_id=person_id,
            viewer_is_user=not contact,
            build_contexts=build_contexts,
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

    def _body_state_for_context(self, strategy: ReplyStrategy) -> str:
        """放进系统上下文的身体状态摘要；没有读数或开关关着时是空字符串。

        扮演模式下换成固定的提示词，没有任何真实读数（D65）。
        """
        if self._config.roleplay_mode:
            return ROLEPLAY_NOTE
        if self._body is None or self._reading is None:
            return ""
        if not self._body.config.state_in_prompt_enabled:
            return ""
        return describe_body_state(self._reading, self._body.config, strategy.describe())

    def _downtime_for_context(self) -> Downtime | None:
        """放进系统上下文的停机情况。睡眠信号关掉后不告诉它睡了多久（04 号文档第 7 节）。"""
        if self._config.roleplay_mode:
            return None  # 扮演模式没有睡眠信号（D65）
        if self._body is not None and not self._body.config.sleep_enabled:
            return None
        return self._downtime

    async def _think(self, job: _ReplyJob) -> _ThoughtOutcome:
        """在后台调用器官：档位允许时先规划，再回复。

        不写任何状态；除了被取消，任何情况都返回结果而不是抛异常。
        规划器出任何问题（调用失败、建议看不懂）都不影响回复：改成合并的一次调用，
        和没有规划器时一样，由回复器自己决定怎么回应（D59）。
        """
        attempt = await self._judge_emotion(job)
        if attempt is not None and attempt.triggers:
            job = self._apply_hormones(job, attempt.triggers)
        plan: Plan | None = None
        forced_reply = False
        if job.strategy.plan_first:
            try:
                plan = await self._plan(job)
            except CallRejectedError as rejection:
                # 网关的拒绝（月度上限、失控拦截）对回复器同样成立，不必再试一次。
                return _ThoughtOutcome(
                    notice=f"没能调用模型，这次没有回复：{rejection.reason}", judge=attempt
                )
            if plan is not None and not plan.should_reply:
                if job.effects is None or not job.effects.must_reply:
                    return _ThoughtOutcome(
                        plan=plan, declined=True, judge=attempt, effects=job.effects
                    )
                # 这场冲突里已经不回过一次：这次必须回，由回复器自己决定怎么回（D69 第 3 条）。
                plan = None
                forced_reply = True
        outcome = await self._reply(job, plan)
        return replace(outcome, judge=attempt, effects=job.effects, forced_reply=forced_reply)

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
                response = await self._organs.call(request)
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

    async def _plan(self, job: _ReplyJob) -> Plan | None:
        """调规划器，返回读懂的建议。调用失败或看不懂时返回 None（记日志）；网关的拒绝照常抛出。"""
        organ_id = self._config.plan_organ
        request = self._organ_request(
            job,
            organ_id=organ_id,
            task="plan",
            prompt=build_plan_prompt(
                job.history,
                job.messages,
                job.user_name,
                self._timezone,
                with_agenda=self._agenda is not None,
            ),
            system_context=job.full_context,
        )
        try:
            response = await self._organs.call(request)
        except CallRejectedError:
            raise
        except OrganCallError as failure:
            logger.warning("规划器调用失败，合并成一次调用照常回复：%s", failure)
            return None
        except Exception:
            logger.exception("调用规划器 %s 时出错，合并成一次调用照常回复", organ_id)
            return None
        plan = parse_plan(response.suggestion)
        if plan is None:
            logger.warning("规划器的建议看不懂，合并成一次调用照常回复")
        return plan

    async def _reply(self, job: _ReplyJob, plan: Plan | None) -> _ThoughtOutcome:
        """调回复器。有规划就按规划的意图和长度写；没有就由它自己决定（合并的一次调用）。"""
        strategy = job.strategy
        if plan is None:
            system_context, intent, length = job.full_context, "", strategy.length_limit
        else:
            system_context, intent = job.replyer_context, plan.intent
            length = (
                plan.length.capped_at(strategy.length_limit)
                if plan.length is not None
                else strategy.length_limit
            )
        request = self._organ_request(
            job,
            organ_id=self._config.reply_organ,
            task="reply",
            prompt=build_reply_prompt(
                job.history,
                job.messages,
                job.user_name,
                self._timezone,
                intent=intent,
                length=length,
            ),
            system_context=system_context,
        )
        try:
            response = await self._organs.call(request)
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
        if not response.suggestion.strip():
            return _ThoughtOutcome(notice="模型返回了空回复，这次没有回复。")
        return _ThoughtOutcome(response=response, plan=plan)

    def _organ_request(
        self,
        job: _ReplyJob,
        *,
        organ_id: str,
        task: str,
        prompt: str,
        system_context: str,
        purpose: CallPurpose = CallPurpose.USER_REPLY,
    ) -> OrganRequest:
        """规划和回复共用的请求格式；主动联系显式传 OPTIONAL（D76）。"""
        return OrganRequest(
            organ_id=organ_id,
            prompt=prompt,
            context={"task": task, "in_reply_to": [record.id for record in job.messages]},
            purpose=purpose,
            system_context=system_context,
            model_preference=job.strategy.model_preference,
        )

    def _on_thought_done(self, _task: asyncio.Task[_ThoughtOutcome]) -> None:
        # 想法有结果了：立即跳一拍去处理，不等下一个定时拍。
        self._wake.set()

    # ---------- 其他 ----------

    async def _wait_for_next_beat(self) -> None:
        """等到有事（新感知、想法有结果、要求停下）或者到了定时拍。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=self.seconds_until_next_beat())
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

    def _deliver(self, target: Perception, content: str, kind: MessageKind) -> bool:
        """把消息交给通道。通道出错只记日志：任何通道出问题都不影响核心（03 号文档）。"""
        sender = self._identity.facts.name if kind is MessageKind.REPLY else NOTICE_SENDER
        message = OutgoingMessage(
            channel=target.channel,
            recipient_id=target.sender_id,
            content=content,
            kind=kind,
            sender_name=sender,
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


def _priority_of(perception: Perception, person: Person | None) -> Priority | None:
    """这条消息按什么优先级排队；不该回复的返回 None（只记录）。

    通道说是别人、档案却说这个账号是用户，两边对不上：宁可不回，也不当成用户或别人处理。
    """
    if perception.trust is TrustLevel.VERIFIED_USER:
        return Priority.USER
    if perception.trust is TrustLevel.VERIFIED_CONTACT:
        if person is not None and person.is_user:
            return None
        return Priority.CONTACT
    return None
