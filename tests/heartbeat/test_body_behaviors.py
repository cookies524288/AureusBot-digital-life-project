"""身体信号改变心跳行为的测试：1.4 的验收（docs/08-roadmap.md，04 号文档第 7 节）。

每个信号开着时有对应的行为，单独关掉后行为消失：
- 疲劳偏高：先等几秒，把同一个人的几条消息攒起来一起回；
- 压力偏高：自检一次，记进时间线并告诉用户，缓过来之后才会再发；
- 睡眠：夜里定时拍放慢；关掉后不再告诉它停机停了多久。
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, ManualClock
from aureus.core import BiologicalState
from aureus.heartbeat import (
    EVENT_MESSAGE,
    EVENT_SELF_CHECK,
    Heartbeat,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.identity import Identity
from aureus.organs import CallAttempt, OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase, TimelineQuery, TimelineRecord, VisibilityScope

pytestmark = pytest.mark.asyncio

DAYTIME = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
NIGHT = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
CALENDAR = BudgetCalendar(UTC)
CHANNEL = "terminal"
TICK = 60.0
NIGHT_TICK = 300.0
BATCH_SECONDS = 20.0


class RecordingOrgans:
    """记下每次收到的请求，每次都回「嗯。」；gate 不为 None 时先等它打开。"""

    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []
        self.gate: asyncio.Event | None = None

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        return OrganResponse(organ_id=request.organ_id, suggestion="嗯。")


@dataclass
class Rig:
    clock: ManualClock
    ledger: Ledger
    timeline: TimelineDatabase
    organs: RecordingOrgans
    heartbeat: Heartbeat
    sent: list[OutgoingMessage] = field(default_factory=list)

    def record_calls(self, count: int, *, succeeded: bool = True) -> None:
        attempt = CallAttempt(
            model="m", provider="p", prompt_tokens=10, completion_tokens=5,
            succeeded=succeeded, failure_reason=None if succeeded else "模型服务 503",
        )  # fmt: skip
        for _ in range(count):
            self.ledger.record("replyer", [attempt])

    def say(self, content: str) -> None:
        assert self.heartbeat.submit(
            Perception(
                channel=CHANNEL,
                sender_id="local",
                content=content,
                visibility=VisibilityScope.USER_ONLY,
                trust=TrustLevel.VERIFIED_USER,
            )
        )

    async def beat(self) -> None:
        """跳一拍，并让出一下执行权，让刚开始的想法有机会去调用器官。"""
        self.heartbeat.beat()
        await asyncio.sleep(0)

    async def settle(self) -> None:
        """等进行中的想法有结果，再跳一拍把它处理掉。"""
        await self.heartbeat.wait_for_thought()
        self.heartbeat.beat()

    def records(self, event_type: str) -> list[TimelineRecord]:
        return list(self.timeline.query(TimelineQuery(event_type=event_type, limit=100)))

    def notices(self) -> list[OutgoingMessage]:
        return [message for message in self.sent if message.kind is MessageKind.NOTICE]


@pytest.fixture
def made() -> Iterator[list[Rig]]:
    rigs: list[Rig] = []
    yield rigs
    for rig in rigs:
        rig.ledger.close()
        rig.timeline.close()


def make_rig(made: list[Rig], tmp_path: Path, *, start: datetime = DAYTIME, **body: Any) -> Rig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = ManualClock(start)
    db_path = tmp_path / "aureus.db"
    ledger = Ledger(db_path, clock, calendar=CALENDAR)
    timeline = TimelineDatabase(db_path, clock)
    ledger.open()
    timeline.open()
    settings = {"fatigue_batch_seconds": BATCH_SECONDS, "night_tick_interval_seconds": NIGHT_TICK}
    config = BodyConfig(**{**settings, **body})
    emergency_stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = RecordingOrgans()
    heartbeat = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(
            {
                "facts": {"name": "小绿", "birthday": "2008-01-01"},
                "user": {"name": "阿明"},
                "persona": {"immutable": "温和。"},
            }
        ),
        bio_state=BiologicalState(clock=clock),
        config=HeartbeatConfig(tick_interval_seconds=TICK),
        permissions=PermissionChecker(emergency_stop),
        emergency_stop=emergency_stop,
        timezone=UTC,
        body=Body(config, None, ledger, clock, CALENDAR),
    )
    rig = Rig(clock, ledger, timeline, organs, heartbeat)
    heartbeat.attach_channel(CHANNEL, rig.sent.append)
    heartbeat.wake_up()
    made.append(rig)
    return rig


# ---------- 疲劳：攒消息 ----------


async def test_high_fatigue_holds_the_reply_then_answers_all_messages_together(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.record_calls(60)  # 窗口里满额，疲劳 100%
    rig.say("在吗")
    await rig.beat()
    assert rig.organs.requests == []  # 先等着
    assert rig.heartbeat.seconds_until_next_beat() == pytest.approx(BATCH_SECONDS)

    rig.clock.advance(timedelta(seconds=8))
    rig.say("我想问个事")
    await rig.beat()
    assert rig.organs.requests == []
    assert rig.heartbeat.seconds_until_next_beat() == pytest.approx(BATCH_SECONDS - 8)

    rig.clock.advance(timedelta(seconds=12))
    rig.heartbeat.beat()
    await rig.settle()

    [request] = rig.organs.requests  # 两条攒成一次调用
    assert "在吗" in request.prompt
    assert "我想问个事" in request.prompt
    assert len(rig.records(EVENT_MESSAGE)) == 3  # 两条用户消息加一条回复


async def test_normal_fatigue_replies_at_once(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    rig.record_calls(5)
    rig.say("在吗")
    await rig.beat()
    assert len(rig.organs.requests) == 1


async def test_disabled_fatigue_replies_at_once_even_when_busy(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, fatigue_enabled=False)
    rig.record_calls(200)
    rig.say("在吗")
    await rig.beat()
    assert len(rig.organs.requests) == 1


# ---------- 压力：自检 ----------


async def test_high_stress_triggers_one_self_check_that_names_the_facts(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.say("你好")
    rig.heartbeat.beat()
    await rig.settle()  # 先有过一次对话，自检才知道告诉谁
    rig.sent.clear()
    rig.record_calls(12, succeeded=False)  # 12/20 = 60%，刚好偏高

    rig.heartbeat.beat()
    rig.heartbeat.beat()  # 第二拍不会再发

    [record] = rig.records(EVENT_SELF_CHECK)
    assert record.visibility is VisibilityScope.PRIVATE
    assert "压力偏高" in record.content
    assert "失败了 12 次" in record.content
    assert "模型服务 503" in record.content
    [notice] = rig.notices()
    assert notice.content == record.content


async def test_self_check_fires_again_only_after_stress_has_eased(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.record_calls(12, succeeded=False)
    rig.heartbeat.beat()
    assert len(rig.records(EVENT_SELF_CHECK)) == 1

    rig.record_calls(20)  # 最近 20 次全成功，压力回到 0
    rig.heartbeat.beat()
    rig.record_calls(12, succeeded=False)
    rig.heartbeat.beat()

    assert len(rig.records(EVENT_SELF_CHECK)) == 2


async def test_a_backlog_of_messages_alone_raises_stress(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    rig.organs.gate = asyncio.Event()  # 模型很慢：后面的消息只能排队
    rig.say("第 0 条")
    rig.heartbeat.beat()
    for index in range(1, 6):
        rig.say(f"第 {index} 条")
    rig.heartbeat.beat()

    [record] = rig.records(EVENT_SELF_CHECK)
    assert record.metadata["backlog"] >= 5
    assert "在排队" in record.content

    rig.organs.gate.set()
    await rig.settle()


async def test_disabled_stress_never_self_checks(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, stress_enabled=False)
    rig.record_calls(20, succeeded=False)
    rig.heartbeat.beat()
    assert rig.records(EVENT_SELF_CHECK) == []


async def test_self_check_is_recorded_even_with_nobody_to_tell(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.record_calls(20, succeeded=False)
    rig.heartbeat.beat()
    assert len(rig.records(EVENT_SELF_CHECK)) == 1
    assert rig.notices() == []


# ---------- 睡眠：夜里放慢、停机时长 ----------


async def test_beats_slow_down_at_night_and_not_by_day(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, start=NIGHT)
    rig.heartbeat.beat()
    assert rig.heartbeat.seconds_until_next_beat() == NIGHT_TICK

    rig.clock.advance(timedelta(hours=5))  # 08:00，不是夜里了
    rig.heartbeat.beat()
    assert rig.heartbeat.seconds_until_next_beat() == TICK


async def test_disabled_sleep_keeps_the_normal_rhythm_at_night(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, start=NIGHT, sleep_enabled=False)
    rig.heartbeat.beat()
    assert rig.heartbeat.seconds_until_next_beat() == TICK


async def test_night_never_makes_beats_faster_than_the_day_rhythm(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, start=NIGHT, night_tick_interval_seconds=1.0)
    rig.heartbeat.beat()
    assert rig.heartbeat.seconds_until_next_beat() == TICK


async def test_a_new_message_wakes_the_heartbeat_at_night(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, start=NIGHT)
    rig.heartbeat.beat()
    rig.say("夜里好")
    # 夜里定时拍很慢（5 分钟），但 submit 会叫醒等待中的心跳，这里不会等到超时。
    await asyncio.wait_for(rig.heartbeat._wait_for_next_beat(), timeout=1.0)


async def test_downtime_reaches_the_reply_context_only_while_sleep_is_enabled(
    made: list[Rig], tmp_path: Path
) -> None:
    awake = make_rig(made, tmp_path / "on")
    awake.say("在吗")
    await awake.beat()
    assert "停机期间" in awake.organs.requests[0].system_context

    ignoring = make_rig(made, tmp_path / "off", sleep_enabled=False)
    ignoring.say("在吗")
    await ignoring.beat()
    assert "停机期间" not in ignoring.organs.requests[0].system_context
