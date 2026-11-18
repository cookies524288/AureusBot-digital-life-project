"""心跳 heartbeat：常驻主循环（02 号文档第 3 节，03 号文档“心跳”一节，D41）。

每一拍按顺序做：量身体 → 对结果（想法有结果了就说出来）→ 收感知（写进时间线）→
定焦点、想一想（有没回复的消息就在后台调用器官）→ 广播认知状态。
急停中（D54）照常收感知、记时间线，但不调用器官、不回复，只发系统通知。

一拍里只有很短的同步操作。慢的器官调用放进后台任务，心跳照常跳；同一时间只想一件事
（D34：前额叶是串行的）。所有状态写入都在拍里完成，后台任务只负责等器官的建议，
有了结果就叫醒心跳，由下一拍处理。
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import Enum, auto
from typing import Any, Protocol

from aureus.body import Body, BodyReading
from aureus.common import Clock, HeartbeatConfig
from aureus.core import BiologicalState, CognitiveObserver, CognitiveState
from aureus.heartbeat.body_summary import describe_body_state
from aureus.heartbeat.context import build_reply_prompt, build_system_context
from aureus.heartbeat.downtime import Downtime, assess_downtime
from aureus.heartbeat.events import (
    EVENT_COGNITIVE_STATE,
    EVENT_EMERGENCY_RESUME,
    EVENT_EMERGENCY_STOP,
    EVENT_MESSAGE,
    EVENT_ORGAN_CALL_ABANDONED,
    EVENT_SELF_CHECK,
    EVENT_SHUT_DOWN,
    EVENT_WOKE_UP,
)
from aureus.heartbeat.perception import (
    MessageKind,
    MessageSink,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.self_check import describe_self_check
from aureus.identity import Identity
from aureus.organs import CallPurpose, OrganCallError, OrganRequest, OrganResponse
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


class OrganCaller(Protocol):
    """心跳调用器官用的对象。核心组装时传入安全网关 SafetyGateway，不传器官总线（D39、D41）。"""

    async def call(self, request: OrganRequest) -> OrganResponse:
        """经过安全检查后调用器官。"""
        ...


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
class _ThoughtOutcome:
    """一次想法的结果：要么有器官的建议，要么有一条要告诉用户的系统通知。"""

    response: OrganResponse | None = None
    notice: str = ""


@dataclass(frozen=True)
class _Thought:
    """进行中的想法：后台任务，和它要回复的消息。"""

    task: asyncio.Task[_ThoughtOutcome]
    replying_to: Sequence[_Heard]


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
        bio_state: BiologicalState,
        config: HeartbeatConfig,
        permissions: PermissionChecker,
        emergency_stop: EmergencyStop,
        timezone: tzinfo | None = None,
        body: Body | None = None,
    ) -> None:
        """创建心跳。时间线要由调用方先打开。

        Args:
            clock: 时钟；所有时间都从它取。
            timeline: 时间线。
            organs: 调用器官用的对象，核心组装时传安全网关。
            identity: 身份事实和四层人设。
            bio_state: 生物状态；心跳只在 advance_biomodel 打开时推进它。
            config: 心跳配置。
            permissions: 权限检查器，要和安全网关用同一个。
            emergency_stop: 急停开关，每拍读一次状态；要和权限检查器里的是同一个。
            timezone: 给人看的时间用哪个时区；不给就用这台电脑设置的时区。
                生物状态没有自己设时区时，日周期也按这个时区算（D42 第 3 条）。
            body: 身体，提供精力读数；不给时精力记为「不限」。
        """
        self._clock = clock
        self._timeline = timeline
        self._organs = organs
        self._identity = identity
        self._bio_state = bio_state
        self._config = config
        self._permissions = permissions
        self._emergency_stop = emergency_stop
        self._halt_status = HaltStatus(halted=False)  # 最近一拍看到的急停状态
        self._timezone = timezone
        if bio_state.timezone is None:
            # 给人看的时间和日周期用同一个时区，免得“早上 8 点”皮质醇却在深夜达到高峰。
            bio_state.timezone = timezone
        self._body = body
        self._reading: BodyReading | None = None  # 最近一次读到的身体信号
        self._stress_alarmed = False  # 这一轮压力偏高的自检通知发过了
        self._last_user_contact: Perception | None = None  # 自检通知发往的地方
        self._observer = CognitiveObserver(clock)
        self._sinks: dict[str, MessageSink] = {}
        self._pending: deque[Perception] = deque()  # 收到了、还没记进时间线
        self._unanswered: list[_Heard] = []  # 记进时间线了、还没开始回复
        self._thought: _Thought | None = None
        self._wake = asyncio.Event()
        self._phase = _Phase.NOT_STARTED
        self._stop_requested = False
        self._last_beat_at: datetime | None = None
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
            lines.append(f"{energy}，信心 {state.planning_confidence:.2f}")
        lines.extend(self._describe_signals())
        hormones = self._bio_state.hormones
        lines.append(
            f"多巴胺 {hormones.dopamine:.2f}，皮质醇 {hormones.cortisol:.2f}，"
            f"血清素 {hormones.serotonin:.2f}，去甲肾上腺素 {hormones.noradrenaline:.2f}"
        )
        if not self._config.advance_biomodel:
            lines.append("生物模型暂停中（advance_biomodel = false）：激素不随时间变化。")
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
        self._downtime = downtime
        self._last_beat_at = now
        self._phase = _Phase.RUNNING
        logger.info("心跳醒来：%s", downtime.describe(self._timezone))
        return downtime

    def beat(self) -> None:
        """跳一拍。只做很短的同步操作；要在事件循环里调用（会创建后台任务）。

        Raises:
            RuntimeError: 心跳没有在运行。
        """
        if self._phase is not _Phase.RUNNING:
            raise RuntimeError("心跳没有在运行，不能跳")
        self._sense_body(self._clock.now())
        self._watch_emergency_stop()
        self._collect_finished_thought()
        self._take_in_perceptions()
        self._read_body()
        self._check_stress()
        self._maybe_start_thought()
        self._broadcast_cognitive_state()

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

    def _sense_body(self, now: datetime) -> None:
        """量身体：按两拍之间过去的时间推进生物模型（开关见 D41 第 2 条、D42 第 8 条）。"""
        previous = self._last_beat_at
        self._last_beat_at = now
        if previous is None or not self._config.advance_biomodel:
            return
        elapsed_seconds = (now - previous).total_seconds()
        # 时钟被往回调过时，经过的时间是负数，这一拍不推进，而不是让激素倒着走。
        if elapsed_seconds > 0:
            self._bio_state.update(elapsed_seconds, now=now)

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
        target = thought.replying_to[-1].perception
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
        self._timeline.append(
            event_type=EVENT_MESSAGE,
            content=text,
            actor_type=ActorType.ME,
            visibility=target.visibility,
            metadata={
                "channel": target.channel,
                "in_reply_to": [heard.record.id for heard in thought.replying_to],
                "organ_id": outcome.response.organ_id,
            },
        )
        self._deliver(target, text, MessageKind.REPLY)

    def _take_in_perceptions(self) -> None:
        """收感知：把新消息记进时间线。先记再出队，记不进去时消息还留在队里。"""
        while self._pending:
            perception = self._pending[0]
            record = self._timeline.append(
                event_type=EVENT_MESSAGE,
                content=perception.content,
                actor_type=_actor_type_of(perception),
                actor_id=perception.sender_id,
                visibility=perception.visibility,
                metadata={"channel": perception.channel, "trust": perception.trust.value},
            )
            self._pending.popleft()
            if perception.trust is TrustLevel.VERIFIED_USER:
                self._unanswered.append(_Heard(perception, record))
                self._last_user_contact = perception
            else:
                # 回复时会带上用户的私聊记录和人设，确认不了身份就不回（朝安全方向倒）。
                logger.info(
                    "收到来自 %s 上未确认身份的账号的消息：只记录，不回复", perception.channel
                )

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
        first = self._unanswered[0].perception
        # 一次只回复同一个通道里同一个人的消息，回复才知道发给谁。
        batch = [
            heard
            for heard in self._unanswered
            if (heard.perception.channel, heard.perception.sender_id)
            == (first.channel, first.sender_id)
        ]
        taken = {heard.record.id for heard in batch}
        # 先组装请求再出队：组装出错时（比如读不了时间线），这些消息还留在队里。
        request = self._reply_request(batch)
        self._unanswered = [heard for heard in self._unanswered if heard.record.id not in taken]
        task = asyncio.create_task(self._think(request), name="aureus-thought")
        task.add_done_callback(self._on_thought_done)
        self._thought = _Thought(task=task, replying_to=batch)

    def _decline_unanswered(self, reason: str) -> None:
        """不允许调用器官时（急停中）：没回复的消息已经在时间线里，这里只通知，之后也不补回复。

        每个对话对象只通知一次，不管他在这一拍里说了几句。
        """
        latest: dict[tuple[str, str], Perception] = {}
        for heard in self._unanswered:
            latest[(heard.perception.channel, heard.perception.sender_id)] = heard.perception
        self._unanswered = []
        for target in latest.values():
            self._deliver(target, f"{reason}。这条消息已经记下，不会回复。", MessageKind.NOTICE)

    def _read_body(self) -> None:
        """读身体信号。账本读不了时沿用上一次的读数，不编一个新的。"""
        if self._body is None:
            return
        backlog = len(self._unanswered) + len(self._pending)
        try:
            self._reading = self._body.read(backlog)
        except Exception:
            # 为什么不抛：这里只是观察自己，读不到不该让心跳停下；真正的安全检查在网关里。
            logger.exception("读不了身体信号，沿用上一次的读数")

    def _check_stress(self) -> None:
        """压力刚变成偏高时做一次自检：记进时间线，并告诉用户（04 号文档第 5 节，D57）。"""
        reading = self._reading
        if reading is None or self._body is None:
            return
        stress = reading.stress
        rearm_below = self._body.config.stress_high_at * _STRESS_REARM_FRACTION
        if not stress.enabled or stress.level < rearm_below:
            self._stress_alarmed = False
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
        if not self._unanswered:
            return 0.0
        batch_seconds = self._body.config.fatigue_batch_seconds
        waited = (self._clock.now() - self._unanswered[0].record.timestamp).total_seconds()
        # 时钟往回调过时 waited 是负数：最多等 batch_seconds，不会无限等下去。
        return max(min(batch_seconds - waited, batch_seconds), 0.0)

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
        state = self._observer.observe(self._bio_state, context, energy)
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

    def _reply_request(self, batch: Sequence[_Heard]) -> OrganRequest:
        """组装回复用的器官请求：人设和醒来信息放系统上下文，对话放提示词。"""
        batch_ids = {heard.record.id for heard in batch}
        # 只带同一可见范围的对话：私聊的回复里不能混进群聊的内容，反过来也一样。
        recent = self._timeline.query(
            TimelineQuery(
                event_type=EVENT_MESSAGE,
                visibility=batch[0].perception.visibility,
                latest=True,
                limit=self._config.recent_message_limit,
            )
        )
        # 按序号排除这次要回复的消息，而不是只取“比它们早的”：想上一件事的时候来的消息，
        # 序号比上一次的回复还小，只取更早的就会漏掉它刚说过的话。
        history = [record for record in recent if record.id not in batch_ids]
        user_name = self._identity.user.name
        return OrganRequest(
            organ_id=self._config.reply_organ,
            prompt=build_reply_prompt(
                history, [heard.record for heard in batch], user_name, self._timezone
            ),
            bio_state=self._bio_state,
            context={"task": "reply", "in_reply_to": [heard.record.id for heard in batch]},
            purpose=CallPurpose.USER_REPLY,
            system_context=build_system_context(
                self._identity,
                self._clock.now(),
                self._downtime_for_context(),
                self._timezone,
                self._body_state_for_context(),
            ),
        )

    def _body_state_for_context(self) -> str:
        """放进系统上下文的身体状态摘要；没有读数或开关关着时是空字符串。"""
        if self._body is None or self._reading is None:
            return ""
        if not self._body.config.state_in_prompt_enabled:
            return ""
        return describe_body_state(self._reading, self._body.config)

    def _downtime_for_context(self) -> Downtime | None:
        """放进系统上下文的停机情况。睡眠信号关掉后不告诉它睡了多久（04 号文档第 7 节）。"""
        if self._body is not None and not self._body.config.sleep_enabled:
            return None
        return self._downtime

    async def _think(self, request: OrganRequest) -> _ThoughtOutcome:
        """在后台调用器官。不写任何状态；除了被取消，任何情况都返回结果而不是抛异常。"""
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
        return _ThoughtOutcome(response=response)

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

    def _deliver(self, target: Perception, content: str, kind: MessageKind) -> None:
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
            return
        try:
            sink(message)
        except Exception:
            logger.exception("通道 %s 没能发出消息", message.channel)


def _actor_type_of(perception: Perception) -> ActorType:
    """确认是用户本人的记成用户，其余记成某个联系人（以后由人物子系统认人）。"""
    if perception.trust is TrustLevel.VERIFIED_USER:
        return ActorType.USER
    return ActorType.PERSON
