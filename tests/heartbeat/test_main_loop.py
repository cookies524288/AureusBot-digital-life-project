"""心跳主循环的测试：一拍一拍地驱动，用手动时钟、真的时间线数据库和假器官，不联网、不花钱。"""

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from aureus.common import AcceleratedClock, HeartbeatConfig, ManualClock, SafetyConfig
from aureus.common import sqlite as sqlite_support
from aureus.core import BiologicalState
from aureus.heartbeat import (
    EVENT_COGNITIVE_STATE,
    EVENT_EMERGENCY_RESUME,
    EVENT_EMERGENCY_STOP,
    EVENT_MESSAGE,
    EVENT_ORGAN_CALL_ABANDONED,
    EVENT_SHUT_DOWN,
    EVENT_WOKE_UP,
    Heartbeat,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.main_loop import SHUTDOWN_INTERRUPTED, TASK_IDLE, TASK_REPLYING
from aureus.identity import Identity
from aureus.organs import CallAttempt, OrganBus, OrganCallError, OrganRequest, OrganResponse
from aureus.safety import (
    EVENT_CALL_REJECTED,
    BudgetExceededError,
    EmergencyStop,
    Ledger,
    PermissionChecker,
    SafetyGateway,
)
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)
CHINA = timezone(timedelta(hours=8))
CHANNEL = "terminal"
USER_ID = "local"


def make_identity() -> Identity:
    return Identity.model_validate(
        {
            "facts": {"name": "小绿", "birthday": "2008-01-01"},
            "user": {"name": "阿明"},
            "persona": {"immutable": "温和，好奇。", "expression_style": "句子短。"},
        }
    )


def said(content: str, trust: TrustLevel = TrustLevel.VERIFIED_USER) -> Perception:
    return Perception(
        channel=CHANNEL,
        sender_id=USER_ID,
        content=content,
        visibility=VisibilityScope.USER_ONLY,
        trust=trust,
    )


class ScriptedOrgans:
    """假的“调用器官的对象”：按顺序给出预设的结果或异常，并记下每次收到的请求。

    gate 不为 None 时，每次调用都先等它被打开，用来模拟慢的模型调用。
    """

    def __init__(self, *outcomes: str | BaseException) -> None:
        self.outcomes: list[str | BaseException] = list(outcomes)
        self.requests: list[OrganRequest] = []
        self.gate: asyncio.Event | None = None
        self.cancelled = False

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        outcome = self.outcomes.pop(0) if self.outcomes else "嗯。"
        if isinstance(outcome, BaseException):
            raise outcome
        return OrganResponse(organ_id=request.organ_id, suggestion=outcome)


@dataclass
class Harness:
    clock: ManualClock
    timeline: TimelineDatabase
    organs: ScriptedOrgans
    bio_state: BiologicalState
    heartbeat: Heartbeat
    emergency_stop: EmergencyStop
    sent: list[OutgoingMessage] = field(default_factory=list)

    def records(self, event_type: str) -> list[TimelineRecord]:
        return list(self.timeline.query(TimelineQuery(event_type=event_type, limit=1000)))

    def all_records(self) -> list[TimelineRecord]:
        return list(self.timeline.query(TimelineQuery(limit=1000)))

    async def exchange(self, *lines: str) -> None:
        """用户说几句，跳一拍开始想，等想完，再跳一拍把回复说出来。"""
        for line in lines:
            assert self.heartbeat.submit(said(line))
        self.heartbeat.beat()
        await self.heartbeat.wait_for_thought()
        self.heartbeat.beat()


class Factory:
    """在同一个数据库文件上，可以先后创建好几个心跳：模拟关掉再重启。"""

    def __init__(self, database_file: Path) -> None:
        self.database_file = database_file
        self.clock = ManualClock(START)
        self.timelines: list[TimelineDatabase] = []

    def make(
        self, organs: ScriptedOrgans | None = None, clock: Any = None, **config: Any
    ) -> Harness:
        clock = clock or self.clock
        timeline = TimelineDatabase(self.database_file, clock=clock)
        timeline.open()
        self.timelines.append(timeline)
        organs = organs or ScriptedOrgans()
        bio_state = BiologicalState(clock=clock)
        emergency_stop = EmergencyStop(self.database_file.with_name("halt.json"), clock)
        heartbeat = Heartbeat(
            clock=clock,
            timeline=timeline,
            organs=organs,
            identity=make_identity(),
            bio_state=bio_state,
            config=HeartbeatConfig(**config),
            permissions=PermissionChecker(emergency_stop),
            emergency_stop=emergency_stop,
            timezone=CHINA,
        )
        harness = Harness(clock, timeline, organs, bio_state, heartbeat, emergency_stop)
        heartbeat.attach_channel(CHANNEL, harness.sent.append)
        return harness

    def close(self) -> None:
        for timeline in self.timelines:
            timeline.close()


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[Factory]:
    made = Factory(tmp_path / "aureus.db")
    yield made
    made.close()  # Windows 上不关数据库，临时文件会被锁住


@pytest.fixture
def harness(factory: Factory) -> Harness:
    made = factory.make()
    made.heartbeat.wake_up()
    return made


# ---------- 醒来 ----------


async def test_first_wake_is_recorded(factory: Factory) -> None:
    harness = factory.make()

    downtime = harness.heartbeat.wake_up()

    assert downtime.is_first_wake
    [record] = harness.records(EVENT_WOKE_UP)
    assert record.visibility is VisibilityScope.PRIVATE
    assert "第一次醒来" in record.content
    assert record.metadata["last_record_at"] is None


async def test_waking_twice_is_a_programming_error(harness: Harness) -> None:
    with pytest.raises(RuntimeError, match="醒来过"):
        harness.heartbeat.wake_up()


async def test_beating_before_waking_is_a_programming_error(factory: Factory) -> None:
    with pytest.raises(RuntimeError, match="没有在运行"):
        factory.make().heartbeat.beat()


# ---------- 收感知、想、说 ----------


async def test_a_message_is_recorded_and_answered(harness: Harness) -> None:
    harness.organs.outcomes = ["早呀。"]

    await harness.exchange("早")

    user, reply = harness.records(EVENT_MESSAGE)
    assert (user.actor_type, user.actor_id, user.content) == (ActorType.USER, USER_ID, "早")
    assert user.visibility is VisibilityScope.USER_ONLY
    assert user.metadata == {"channel": CHANNEL, "trust": "verified_user"}
    assert (reply.actor_type, reply.content) == (ActorType.ME, "早呀。")
    assert reply.visibility is VisibilityScope.USER_ONLY
    assert reply.metadata["in_reply_to"] == [user.id]
    assert harness.sent == [
        OutgoingMessage(CHANNEL, USER_ID, "早呀。", MessageKind.REPLY, sender_name="小绿")
    ]


async def test_the_request_carries_persona_wakeup_history_and_new_message(
    harness: Harness,
) -> None:
    await harness.exchange("早")
    await harness.exchange("今天面试被拒了")

    request = harness.organs.requests[-1]
    assert request.organ_id == "replyer"
    assert request.bio_state is harness.bio_state
    assert "你是小绿。" in request.system_context
    assert "【表达风格】\n句子短。" in request.system_context
    assert "第一次醒来" in request.system_context
    history, new = request.prompt.split("刚收到、还没回复的消息：")
    assert "阿明：早" in history
    assert "你：嗯。" in history
    assert "今天面试被拒了" in new
    assert "今天面试被拒了" not in history


async def test_reply_organ_comes_from_config(factory: Factory) -> None:
    harness = factory.make(reply_organ="chatter")
    harness.heartbeat.wake_up()

    await harness.exchange("你好")

    assert harness.organs.requests[0].organ_id == "chatter"


async def test_history_is_limited_and_excludes_other_visibility(factory: Factory) -> None:
    harness = factory.make(recent_message_limit=3)
    harness.heartbeat.wake_up()
    for index in range(3):
        await harness.exchange(f"第{index}句")
    harness.timeline.append(
        EVENT_MESSAGE, "群里的话", ActorType.PERSON, "someone", VisibilityScope.GROUP
    )

    await harness.exchange("最后一句")

    prompt = harness.organs.requests[-1].prompt
    assert "群里的话" not in prompt
    # 最近 3 条里有这次的新消息，所以之前的对话只剩 2 条。
    assert "第2句" in prompt
    assert "第1句" not in prompt


async def test_messages_arriving_while_thinking_are_answered_together(harness: Harness) -> None:
    harness.organs.gate = asyncio.Event()
    harness.heartbeat.submit(said("在吗"))
    harness.heartbeat.beat()
    await asyncio.sleep(0)  # 让后台的想法开始等
    harness.heartbeat.submit(said("想问你个事"))
    harness.heartbeat.submit(said("算了，明天说"))
    harness.heartbeat.beat()

    assert len(harness.organs.requests) == 1  # 同一时间只想一件事
    assert len(harness.records(EVENT_MESSAGE)) == 3  # 但新消息照样马上记进时间线

    harness.organs.gate.set()
    await harness.heartbeat.wait_for_thought()
    harness.heartbeat.beat()  # 说出第一次的回复，并开始想后两句
    await harness.heartbeat.wait_for_thought()
    harness.heartbeat.beat()

    assert len(harness.organs.requests) == 2
    history, second = harness.organs.requests[1].prompt.split("刚收到、还没回复的消息：")
    assert "想问你个事" in second
    assert "算了，明天说" in second
    # 它对“在吗”的回复，序号比后两句大，也要出现在之前的对话里。
    assert "你：嗯。" in history
    assert "想问你个事" not in history
    reply = harness.records(EVENT_MESSAGE)[-1]
    assert reply.actor_type is ActorType.ME
    assert len(reply.metadata["in_reply_to"]) == 2


async def test_unverified_sender_is_recorded_but_not_answered(harness: Harness) -> None:
    harness.heartbeat.submit(said("你好", trust=TrustLevel.UNVERIFIED))
    harness.heartbeat.beat()

    [record] = harness.records(EVENT_MESSAGE)
    assert record.actor_type is ActorType.PERSON
    assert record.metadata["trust"] == "unverified"
    assert not harness.heartbeat.is_thinking
    assert harness.organs.requests == []


async def test_message_from_unattached_channel_is_rejected(harness: Harness) -> None:
    stranger = Perception("qq", "1", "hi", VisibilityScope.USER_ONLY, TrustLevel.UNVERIFIED)
    with pytest.raises(ValueError, match="qq"):
        harness.heartbeat.submit(stranger)


async def test_attaching_the_same_channel_twice_is_rejected(harness: Harness) -> None:
    with pytest.raises(ValueError, match=CHANNEL):
        harness.heartbeat.attach_channel(CHANNEL, harness.sent.append)


@pytest.mark.parametrize("bad", ["", "   "])
async def test_empty_perception_is_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        said(bad)


# ---------- 失败时：发系统通知，不替它编回复（D35） ----------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (BudgetExceededError("replyer", "本月已用 10 token，达到上限 10"), "达到上限"),
        (OrganCallError("replyer", ["big：超时"]), "模型调用失败"),
        (RuntimeError("bug"), "RuntimeError"),
        ("   ", "空回复"),
    ],
)
async def test_failures_become_a_notice_not_a_reply(
    harness: Harness, outcome: str | BaseException, expected: str
) -> None:
    harness.organs.outcomes = [outcome]

    await harness.exchange("在吗")

    [notice] = harness.sent
    assert notice.kind is MessageKind.NOTICE
    assert notice.sender_name == "系统"
    assert expected in notice.content
    assert [r.actor_type for r in harness.records(EVENT_MESSAGE)] == [ActorType.USER]
    assert not harness.heartbeat.is_thinking


async def test_a_broken_channel_does_not_stop_the_heartbeat(factory: Factory) -> None:
    harness = factory.make()
    harness.heartbeat.wake_up()

    def broken(_message: OutgoingMessage) -> None:
        raise OSError("终端被关了")

    harness.heartbeat._sinks[CHANNEL] = broken  # 换成会出错的通道
    await harness.exchange("你好")
    harness.heartbeat.beat()

    assert harness.records(EVENT_MESSAGE)[-1].actor_type is ActorType.ME


class TokenSpendingReplyer:
    """会用 token 的假器官：每次回复报告 6 个 token。"""

    organ_id = "replyer"

    async def process(self, request: OrganRequest) -> OrganResponse:
        attempt = CallAttempt("fake", "fake", prompt_tokens=4, completion_tokens=2)
        return OrganResponse(self.organ_id, suggestion="嗯。", attempts=[attempt])


async def test_with_the_real_gateway_a_budget_rejection_reaches_the_user(
    factory: Factory, tmp_path: Path
) -> None:
    # 验收 3 的一部分：把预算上限调得很低，超限后拒绝调用，用户看到的是系统通知。
    database = tmp_path / "aureus.db"
    ledger = Ledger(database, clock=factory.clock)
    ledger.open()
    try:
        bus = OrganBus()
        bus.register(TokenSpendingReplyer())
        timeline = TimelineDatabase(database, clock=factory.clock)
        timeline.open()
        factory.timelines.append(timeline)
        emergency_stop = EmergencyStop(tmp_path / "halt.json", factory.clock)
        permissions = PermissionChecker(emergency_stop)
        gateway = SafetyGateway(
            bus,
            ledger,
            timeline,
            factory.clock,
            SafetyConfig(monthly_token_limit=10),
            permissions,
        )
        heartbeat = Heartbeat(
            clock=factory.clock,
            timeline=timeline,
            organs=gateway,
            identity=make_identity(),
            bio_state=BiologicalState(clock=factory.clock),
            config=HeartbeatConfig(),
            permissions=permissions,
            emergency_stop=emergency_stop,
            timezone=CHINA,
        )
        sent: list[OutgoingMessage] = []
        heartbeat.attach_channel(CHANNEL, sent.append)
        heartbeat.wake_up()

        for line in ("你好", "在吗", "还在吗"):
            heartbeat.submit(said(line))
            heartbeat.beat()
            await heartbeat.wait_for_thought()
            heartbeat.beat()

        # 第 1、2 次各用 6 个；第 2 次之后已用 12，超过上限 10，第 3 次被拒绝。
        assert [m.kind for m in sent] == [MessageKind.REPLY, MessageKind.REPLY, MessageKind.NOTICE]
        assert "达到上限 10" in sent[-1].content
        assert ledger.tokens_used_in_month(factory.clock.now()) == 12
        rejected = timeline.query(TimelineQuery(event_type=EVENT_CALL_REJECTED))
        assert [r.metadata["kind"] for r in rejected] == ["BudgetExceededError"]
    finally:
        ledger.close()


# ---------- 认知广播（D34、D41 第 4 条） ----------


async def test_cognitive_state_is_written_only_when_the_statement_changes(
    harness: Harness,
) -> None:
    for _ in range(5):
        harness.heartbeat.beat()
    idle = harness.records(EVENT_COGNITIVE_STATE)
    assert len(idle) == 1
    assert idle[0].content.startswith(f"我现在在{TASK_IDLE}")
    assert idle[0].visibility is VisibilityScope.PRIVATE
    assert idle[0].metadata["current_task"] == TASK_IDLE

    await harness.exchange("你好")
    harness.heartbeat.beat()

    tasks = [r.metadata["current_task"] for r in harness.records(EVENT_COGNITIVE_STATE)]
    assert tasks == [TASK_IDLE, TASK_REPLYING, TASK_IDLE]


async def test_cognitive_state_is_always_available_in_memory(harness: Harness) -> None:
    assert harness.heartbeat.cognitive_state is None
    harness.heartbeat.beat()

    state = harness.heartbeat.cognitive_state
    assert state is not None
    assert state.last_updated == harness.clock.now()
    assert state.self_awareness_statement() in harness.heartbeat.describe_state()


async def test_broadcast_switch_turns_off_timeline_records(factory: Factory) -> None:
    harness = factory.make(broadcast_cognitive_state=False)
    harness.heartbeat.wake_up()

    harness.heartbeat.beat()

    assert harness.records(EVENT_COGNITIVE_STATE) == []
    assert harness.heartbeat.cognitive_state is not None  # 内存里照样有


# ---------- 生物模型开关（D41 第 2 条） ----------


def body(state: BiologicalState) -> tuple[float, ...]:
    hormones = state.hormones
    return (state.energy, hormones.dopamine, hormones.cortisol, state.social_deficit)


class UpdateRecorder:
    """代替 BiologicalState.update，记下每次推进了多少秒、到哪个时刻。"""

    def __init__(self) -> None:
        self.steps: list[float] = []
        self.ends: list[datetime | None] = []

    def __call__(self, dt: float, now: datetime | None = None) -> None:
        self.steps.append(dt)
        self.ends.append(now)


async def test_biomodel_is_frozen_when_switched_off(factory: Factory) -> None:
    harness = factory.make(advance_biomodel=False)
    harness.heartbeat.wake_up()
    before = body(harness.bio_state)

    harness.clock.advance(timedelta(hours=2))
    harness.heartbeat.beat()
    await harness.exchange("在吗")  # 关着时谈话也不让社交需求回落（D42 第 8 条）

    assert body(harness.bio_state) == before
    assert "生物模型暂停中" in harness.heartbeat.describe_state()


async def test_biomodel_advances_by_default(harness: Harness) -> None:
    before = harness.bio_state.social_deficit

    harness.clock.advance(timedelta(hours=2))
    harness.heartbeat.beat()

    assert harness.bio_state.social_deficit > before  # 社交需求随时间上涨（D42 第 5 条）
    assert "生物模型暂停中" not in harness.heartbeat.describe_state()


async def test_a_delivered_reply_relieves_social_need(harness: Harness) -> None:
    harness.bio_state.social_deficit = 0.5

    await harness.exchange("在吗")

    assert len(harness.sent) == 1
    # 时钟没走，所以只有谈话带来的回落（D42 第 7 条）。
    expected = 0.5 - harness.bio_state.social_relief_per_conversation
    assert harness.bio_state.social_deficit == pytest.approx(expected)


async def test_a_failed_reply_does_not_count_as_a_conversation(factory: Factory) -> None:
    harness = factory.make(ScriptedOrgans(OrganCallError("replyer", ["big：超时"])))
    harness.heartbeat.wake_up()
    harness.bio_state.social_deficit = 0.5

    await harness.exchange("在吗")

    assert harness.sent[0].kind is MessageKind.NOTICE
    assert harness.bio_state.social_deficit == 0.5


async def test_heartbeat_timezone_drives_the_circadian_phase(factory: Factory) -> None:
    harness = factory.make()
    assert harness.bio_state.timezone is CHINA  # 生物状态没设时区时，用心跳的（D42 第 3 条）


async def test_biomodel_advances_by_clock_time_when_enabled(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = factory.make(advance_biomodel=True)
    recorder = UpdateRecorder()
    steps = recorder.steps
    monkeypatch.setattr(harness.bio_state, "update", recorder)
    harness.heartbeat.wake_up()

    harness.clock.advance(timedelta(seconds=90))
    harness.heartbeat.beat()
    harness.heartbeat.beat()  # 时间没走：不推进
    harness.clock.advance(timedelta(minutes=5))
    harness.heartbeat.beat()

    assert steps == [90.0, 300.0]
    assert recorder.ends[-1] == harness.clock.now()  # 按心跳量到的时刻推进，不各自读时钟


async def test_biomodel_does_not_run_backwards_when_the_clock_jumps_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SettableClock:
        def __init__(self) -> None:
            self.current = START

        def now(self) -> datetime:
            return self.current

    clock = SettableClock()
    made = Factory(tmp_path / "aureus.db")
    try:
        harness = made.make(clock=clock, advance_biomodel=True)
        recorder = UpdateRecorder()
        steps = recorder.steps
        monkeypatch.setattr(harness.bio_state, "update", recorder)
        harness.heartbeat.wake_up()

        clock.current = START - timedelta(hours=1)
        harness.heartbeat.beat()
        clock.current = START - timedelta(minutes=59)
        harness.heartbeat.beat()

        assert steps == [60.0]  # 往回跳的那一拍不推进，之后从新的时间接着算
    finally:
        made.close()


# ---------- 关闭与重启 ----------


async def test_restart_remembers_the_conversation_and_how_long_it_was_off(
    factory: Factory,
) -> None:
    first = factory.make()
    first.heartbeat.wake_up()
    await first.exchange("我叫阿明，今天面试被拒了")
    await first.heartbeat.shut_down()

    factory.clock.advance(timedelta(hours=10, minutes=5))
    second = factory.make()
    downtime = second.heartbeat.wake_up()
    await second.exchange("还记得我刚才说了什么吗")

    assert downtime.shutdown_recorded
    assert downtime.duration == timedelta(hours=10, minutes=5)
    request = second.organs.requests[0]
    assert "停了 10 小时 5 分钟" in request.system_context
    assert "今天面试被拒了" in request.prompt.split("刚收到、还没回复的消息：")[0]


async def test_shutdown_is_recorded_once(harness: Harness) -> None:
    await harness.heartbeat.shut_down()
    await harness.heartbeat.shut_down()

    [record] = harness.records(EVENT_SHUT_DOWN)
    assert record.metadata == {"reason": "按要求关闭", "unanswered_messages": 0}
    assert not harness.heartbeat.submit(said("还在吗"))


async def test_shutdown_before_waking_writes_nothing(factory: Factory) -> None:
    harness = factory.make()

    await harness.heartbeat.shut_down()

    assert harness.all_records() == []


async def test_without_a_shutdown_record_the_next_wake_gives_an_upper_bound(
    factory: Factory,
) -> None:
    first = factory.make()
    first.heartbeat.wake_up()
    await first.exchange("你好")
    # 不调用 shut_down：模拟断电或被强行结束。

    factory.clock.advance(timedelta(days=2))
    downtime = factory.make().heartbeat.wake_up()

    assert not downtime.shutdown_recorded
    assert downtime.duration == timedelta(days=2)


async def test_shutdown_waits_for_a_thought_that_finishes_in_time(harness: Harness) -> None:
    harness.organs.gate = asyncio.Event()
    harness.organs.outcomes = ["晚安。"]
    harness.heartbeat.submit(said("我去睡了"))
    harness.heartbeat.beat()

    shutting = asyncio.create_task(harness.heartbeat.shut_down())
    await asyncio.sleep(0)
    harness.organs.gate.set()
    await shutting

    assert harness.sent[-1].content == "晚安。"
    *_, reply, shut = harness.all_records()
    assert (reply.event_type, reply.content) == (EVENT_MESSAGE, "晚安。")
    assert shut.event_type == EVENT_SHUT_DOWN


async def test_shutdown_cancels_a_thought_that_takes_too_long(factory: Factory) -> None:
    harness = factory.make(shutdown_grace_seconds=0)
    harness.heartbeat.wake_up()
    harness.organs.gate = asyncio.Event()  # 永远不打开
    harness.heartbeat.submit(said("在吗"))
    harness.heartbeat.beat()
    await asyncio.sleep(0)

    await harness.heartbeat.shut_down()

    assert harness.organs.cancelled
    [abandoned] = harness.records(EVENT_ORGAN_CALL_ABANDONED)
    assert "没有记进账本" in abandoned.content
    assert abandoned.metadata["organ_id"] == "replyer"
    assert harness.all_records()[-1].event_type == EVENT_SHUT_DOWN
    assert harness.sent[-1].kind is MessageKind.NOTICE


async def test_messages_still_queued_at_shutdown_are_recorded(harness: Harness) -> None:
    harness.heartbeat.submit(said("最后一句"))

    await harness.heartbeat.shut_down()

    [message] = harness.records(EVENT_MESSAGE)
    assert message.content == "最后一句"
    assert harness.records(EVENT_SHUT_DOWN)[0].metadata["unanswered_messages"] == 1
    assert harness.organs.requests == []  # 关闭时不再开始新的想法


async def test_shutdown_does_not_raise_when_the_timeline_is_gone(harness: Harness) -> None:
    harness.timeline.close()

    await harness.heartbeat.shut_down()  # 只记日志，不掩盖原来让它停下的错误

    harness.timeline.open()
    assert harness.records(EVENT_SHUT_DOWN) == []


# ---------- run()：真正的主循环 ----------


async def wait_until(condition: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_run_answers_immediately_and_stops_on_request(factory: Factory) -> None:
    # 定时拍间隔很长：能马上回复，说明是被消息和想法的结果叫醒的，而不是等定时拍。
    harness = factory.make(tick_interval_seconds=3600)
    running = asyncio.create_task(harness.heartbeat.run())
    await wait_until(lambda: harness.heartbeat.downtime is not None)

    harness.heartbeat.submit(said("你好"))
    await wait_until(lambda: harness.sent)
    harness.heartbeat.request_stop()
    await asyncio.wait_for(running, timeout=5)

    events = [r.event_type for r in harness.all_records()]
    assert events[0] == EVENT_WOKE_UP
    assert events[-1] == EVENT_SHUT_DOWN
    assert harness.sent[0].kind is MessageKind.REPLY


async def test_cancelled_run_still_shuts_down(factory: Factory) -> None:
    harness = factory.make(tick_interval_seconds=3600)
    running = asyncio.create_task(harness.heartbeat.run())
    await wait_until(lambda: harness.heartbeat.cognitive_state is not None)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    [record] = harness.records(EVENT_SHUT_DOWN)
    assert record.metadata["reason"] == SHUTDOWN_INTERRUPTED


async def test_accelerated_clock_puts_accelerated_time_on_the_timeline(
    factory: Factory,
) -> None:
    real = ManualClock(START)
    accelerated = AcceleratedClock(start=START, speed=3600, real_clock=real)
    harness = factory.make(clock=accelerated)
    harness.heartbeat.wake_up()

    real.advance(timedelta(seconds=2))  # 真实 2 秒 = 加速后 2 小时
    await harness.exchange("你好")

    user = harness.records(EVENT_MESSAGE)[0]
    assert user.timestamp == START + timedelta(hours=2)


async def test_if_the_request_cannot_be_built_the_messages_stay_queued(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.heartbeat.submit(said("你好"))
    harness.heartbeat.beat()  # 记进时间线，开始想
    await harness.heartbeat.wait_for_thought()
    harness.heartbeat.submit(said("还在吗"))

    def broken(_batch: object) -> OrganRequest:
        raise RuntimeError("读不了时间线")

    monkeypatch.setattr(harness.heartbeat, "_reply_request", broken)
    with pytest.raises(RuntimeError):
        harness.heartbeat.beat()
    monkeypatch.undo()

    harness.heartbeat.beat()  # 恢复之后，那条消息还在，照样回复
    await harness.heartbeat.wait_for_thought()
    harness.heartbeat.beat()
    assert "还在吗" in harness.organs.requests[-1].prompt.split("刚收到、还没回复的消息：")[1]


async def test_calling_shut_down_wakes_a_waiting_run_loop(factory: Factory) -> None:
    harness = factory.make(tick_interval_seconds=3600)
    running = asyncio.create_task(harness.heartbeat.run())
    await wait_until(lambda: harness.heartbeat.cognitive_state is not None)

    await harness.heartbeat.shut_down()
    await asyncio.wait_for(running, timeout=5)  # 不用等一小时的定时拍

    assert len(harness.records(EVENT_SHUT_DOWN)) == 1


# ---------- 写库被锁住（审查 M2） ----------


async def test_a_locked_database_stops_the_beat_instead_of_losing_the_message(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 别的进程占着写锁时，这一拍的写入在有限时间内失败并抛出，由 run() 停下来收尾；
    # 不会悄悄跳过写入、带着没记全的经历继续跑。
    monkeypatch.setattr(sqlite_support, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.1)
    harness = factory.make()
    harness.heartbeat.wake_up()
    holder = sqlite3.connect(str(factory.database_file), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert harness.heartbeat.submit(said("你好"))
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            harness.heartbeat.beat()
        assert time.monotonic() - started < 1.0
    finally:
        holder.close()


# ---------- 急停（1.1，D54） ----------


def notices(harness: Harness) -> list[str]:
    return [m.content for m in harness.sent if m.kind is MessageKind.NOTICE]


async def test_halted_messages_are_recorded_but_not_answered(harness: Harness) -> None:
    # 验收 8：急停后发消息，时间线有记录、没有调用器官、收到「急停中」通知。
    harness.heartbeat.halt("测试急停")

    assert harness.heartbeat.submit(said("在吗"))
    harness.heartbeat.beat()

    [user] = harness.records(EVENT_MESSAGE)
    assert user.content == "在吗"
    assert not harness.heartbeat.is_thinking
    assert harness.organs.requests == []
    [notice] = notices(harness)
    assert "急停中" in notice
    assert "测试急停" in notice
    assert [m for m in harness.sent if m.kind is MessageKind.REPLY] == []


async def test_halting_and_resuming_are_recorded_once_each(harness: Harness) -> None:
    harness.heartbeat.halt("先停一下")
    harness.heartbeat.beat()
    harness.heartbeat.beat()  # 再跳一拍不重复记
    [stopped] = harness.records(EVENT_EMERGENCY_STOP)
    assert stopped.visibility is VisibilityScope.PRIVATE
    assert stopped.metadata["reason"] == "先停一下"

    harness.heartbeat.resume()
    harness.heartbeat.beat()
    [resumed] = harness.records(EVENT_EMERGENCY_RESUME)
    assert resumed.content == "急停解除"


async def test_replies_resume_after_the_halt_is_lifted(harness: Harness) -> None:
    harness.organs.outcomes = ["我回来了。"]
    harness.heartbeat.halt()
    harness.heartbeat.submit(said("在吗"))
    harness.heartbeat.beat()
    assert harness.organs.requests == []

    harness.heartbeat.resume()
    await harness.exchange("现在呢")

    assert len(harness.organs.requests) == 1
    # 急停时的那句不补回复：请求里只有解除后才说的这一句。
    assert "在吗" not in harness.organs.requests[0].prompt.split("刚收到、还没回复的消息：")[1]
    assert [m.content for m in harness.sent if m.kind is MessageKind.REPLY] == ["我回来了。"]


async def test_one_notice_per_sender_even_if_they_say_several_things(harness: Harness) -> None:
    harness.heartbeat.halt()
    for line in ("一", "二", "三"):
        harness.heartbeat.submit(said(line))
    harness.heartbeat.beat()

    assert len(harness.records(EVENT_MESSAGE)) == 3
    assert len(notices(harness)) == 1


async def test_halt_cancels_a_call_in_progress_and_discards_the_answer(harness: Harness) -> None:
    harness.organs.gate = asyncio.Event()
    harness.heartbeat.submit(said("想个很久的问题"))
    harness.heartbeat.beat()
    assert harness.heartbeat.is_thinking
    await asyncio.sleep(0)  # 让器官调用真的开始，停在 gate 上

    harness.heartbeat.halt()
    harness.heartbeat.beat()  # 看到急停，取消进行中的调用
    await harness.heartbeat.wait_for_thought()
    harness.heartbeat.beat()  # 收尾

    assert harness.organs.cancelled
    assert not harness.heartbeat.is_thinking
    assert len(harness.records(EVENT_ORGAN_CALL_ABANDONED)) == 1
    assert [m for m in harness.sent if m.kind is MessageKind.REPLY] == []
    assert any("急停" in text for text in notices(harness))
    # 只有用户那一条，没有它的回复。
    assert [r.actor_type for r in harness.records(EVENT_MESSAGE)] == [ActorType.USER]


async def test_an_answer_that_arrives_during_a_halt_is_not_spoken(harness: Harness) -> None:
    # 想法已经做完、还没说出口时急停：回复权限检查把它拦下，不进时间线。
    harness.organs.outcomes = ["这句不该说出口"]
    harness.heartbeat.submit(said("你好"))
    harness.heartbeat.beat()
    await harness.heartbeat.wait_for_thought()

    harness.heartbeat.halt()
    harness.heartbeat.beat()

    assert [m for m in harness.sent if m.kind is MessageKind.REPLY] == []
    assert [r.actor_type for r in harness.records(EVENT_MESSAGE)] == [ActorType.USER]


async def test_describe_state_mentions_the_halt(harness: Harness) -> None:
    harness.heartbeat.halt("看看状态")
    harness.heartbeat.beat()
    assert "急停中：看看状态" in harness.heartbeat.describe_state()
    harness.heartbeat.resume()
    harness.heartbeat.beat()
    assert "急停中" not in harness.heartbeat.describe_state()


async def test_a_halt_set_from_another_process_is_noticed(harness: Harness) -> None:
    # aureus halt 是另一个进程写的标记文件，心跳不经过 halt() 也要发现。
    harness.emergency_stop.engage("外面设的")
    harness.heartbeat.submit(said("在吗"))
    harness.heartbeat.beat()

    assert harness.organs.requests == []
    assert len(harness.records(EVENT_EMERGENCY_STOP)) == 1
