"""在假时钟下验证回复分段的外发时刻、会话顺序和后续规划上下文（3.12）。"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aureus.attention import WorkingMemory
from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import (
    EVENT_MESSAGE,
    Heartbeat,
    MessageKind,
    MessageRoute,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import ACTION_REPLY_TO_USER, EmergencyStop, PermissionChecker, PermissionDecision
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

pytestmark = pytest.mark.asyncio

CHANNEL = "terminal"
REPLY = "今天面试被拒了。别难过！我陪你聊聊？"
PIECES = ["今天面试被拒了。", "别难过！", "我陪你聊聊？"]


class FixedOrgans:
    """捕获真实规划的请求，按顺序给出准备好的回复。"""

    def __init__(self, replies: list[str]) -> None:
        self.plans: list[OrganRequest] = []
        self.replies = iter(replies)

    async def call(self, request: OrganRequest) -> OrganResponse:
        if request.context["task"] == "plan":
            self.plans.append(request)
            return OrganResponse(request.organ_id, '{"reply":true,"intent":"回应"}')
        return OrganResponse(request.organ_id, next(self.replies))


class Rig:
    def __init__(
        self,
        timeline: TimelineDatabase,
        heartbeat: Heartbeat,
        clock: ManualClock,
        organs: FixedOrgans,
        permissions: PermissionChecker,
    ) -> None:
        self.timeline = timeline
        self.heartbeat = heartbeat
        self.clock = clock
        self.organs = organs
        self.permissions = permissions
        self.started_at = clock.now()
        self.sent: list[OutgoingMessage] = []
        self.delivery_times: list[datetime] = []
        heartbeat.attach_channel(CHANNEL, self.capture)
        heartbeat.wake_up()

    def capture(self, message: OutgoingMessage) -> None:
        self.sent.append(message)
        if message.kind == MessageKind.REPLY:
            self.delivery_times.append(self.clock.now())

    async def say(
        self, content: str, *, sender_id: str = "local", route: MessageRoute | None = None
    ) -> None:
        assert self.heartbeat.submit(
            Perception(
                channel=CHANNEL,
                sender_id=sender_id,
                content=content,
                visibility=VisibilityScope.GROUP if route else VisibilityScope.USER_ONLY,
                trust=TrustLevel.VERIFIED_USER,
                route=route,
            )
        )
        self.heartbeat.beat()
        await self.heartbeat.wait_for_thought()
        self.heartbeat.beat()

    def replies(self) -> list[str]:
        return [m.content for m in self.sent if m.kind is MessageKind.REPLY]

    def advance(self, seconds: float) -> None:
        self.clock.advance(timedelta(seconds=seconds))
        self.heartbeat.beat()


@pytest.fixture
def made(tmp_path: Path) -> Iterator[list[TimelineDatabase]]:
    timelines: list[TimelineDatabase] = []
    yield timelines
    for timeline in timelines:
        timeline.close()


def make_rig(
    made: list[TimelineDatabase], tmp_path: Path, *, replies: list[str] | None = None, **config: Any
) -> Rig:
    clock = ManualClock(datetime(2026, 11, 17, 12, 0, tzinfo=UTC))
    timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
    timeline.open()
    made.append(timeline)
    emergency_stop = EmergencyStop(tmp_path / "halt.json", clock)
    permissions = PermissionChecker(emergency_stop)
    heartbeat_config = HeartbeatConfig(**config)
    organs = FixedOrgans(replies or [REPLY])
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
        config=heartbeat_config,
        permissions=permissions,
        emergency_stop=emergency_stop,
        timezone=UTC,
        working_memory=WorkingMemory(timeline, heartbeat_config),
    )
    return Rig(timeline, heartbeat, clock, organs, permissions)


async def test_a_reply_is_sent_as_several_short_messages(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    await rig.say("我今天不太顺")

    assert rig.replies() == PIECES[:1]
    rig.advance(4)
    rig.advance(5)
    assert rig.replies() == PIECES
    assert all(m.sender_name == "小绿" for m in rig.sent)


async def test_the_timeline_keeps_the_whole_reply_as_one_record(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, typing_delay_enabled=False)
    await rig.say("我今天不太顺")

    records = rig.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=100))
    mine = [r for r in records if r.actor_type is ActorType.ME]
    assert [r.content for r in mine] == ["\n".join(PIECES)]
    assert mine[0].metadata["reply_segments"] == PIECES
    assert [message.timeline_id for message in rig.sent] == [mine[0].id] * len(PIECES)
    assert all(message.timestamp == mine[0].timestamp for message in rig.sent)


async def test_switching_splitting_off_sends_the_whole_reply(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, split_reply_enabled=False)
    await rig.say("我今天不太顺")

    assert rig.replies() == [REPLY]


async def test_the_message_limit_is_respected(made: list[TimelineDatabase], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, max_reply_messages=2, typing_delay_enabled=False)
    await rig.say("我今天不太顺")

    assert len(rig.replies()) == 2
    assert "".join(rig.replies()).count("别难过") == 1


async def test_a_limit_of_one_sends_one_message(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, max_reply_messages=1)
    await rig.say("我今天不太顺")

    assert rig.replies() == [REPLY]


async def test_typing_uses_han_other_and_each_jitter_draw(
    made: list[TimelineDatabase], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draws = iter([0.3, 1.7])

    def jitter(lower: float, upper: float) -> float:
        assert (lower, upper) == (0.3, 1.7)
        return next(draws)

    monkeypatch.setattr("aureus.heartbeat.reply_outbox.uniform", jitter)
    rig = make_rig(made, tmp_path, replies=["开头<n>汉𠀀A!<n>emoji🙂"])
    await rig.say("开始")
    assert rig.replies() == ["开头"]
    assert rig.heartbeat.seconds_until_next_beat() == pytest.approx(1.3)
    rig.advance(1.299)
    assert rig.replies() == ["开头"]
    rig.advance(0.001)
    assert rig.replies() == ["开头", "汉𠀀A!"]
    assert rig.heartbeat.seconds_until_next_beat() == pytest.approx(2.3)
    rig.advance(2.3)
    assert rig.replies() == ["开头", "汉𠀀A!", "emoji🙂"]
    assert rig.delivery_times == [
        rig.started_at + timedelta(seconds=seconds) for seconds in (0, 1.3, 3.6)
    ]


async def test_next_planner_sees_all_parts_and_same_conversation_stays_in_order(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(
        made,
        tmp_path,
        replies=["首段，<n><!>随时都可以\n  继续说<n>收尾,", "新回复<n>稍等"],
        typing_han_seconds=0,
        typing_other_seconds=0,
        typing_jitter_min_seconds=2,
        typing_jitter_max_seconds=2,
    )
    group = MessageRoute("bot", "group", "group-one", "first")
    await rig.say("第一次来信", sender_id="first-person", route=group)
    assert rig.replies() == ["首段"]
    rig.advance(1)
    second = MessageRoute("bot", "group", "group-one", "second")
    await rig.say("第二次来信", sender_id="another-person", route=second)
    assert rig.replies() == ["首段"]
    prompt = rig.organs.plans[1].prompt
    assert "你（第 1/3 段）：首段" in prompt
    assert r"你（第 2/3 段）：随时都可以\n  继续说" in prompt
    assert "你（第 3/3 段）：收尾" in prompt
    assert "第二次来信" in prompt
    assert all(marker not in prompt for marker in ("首段，", "收尾,", "<n>", "<!>"))
    rig.advance(1)
    assert rig.replies() == ["首段", "随时都可以\n  继续说"]
    rig.advance(2)
    assert rig.replies() == ["首段", "随时都可以\n  继续说", "收尾", "新回复"]
    rig.advance(2)
    assert rig.replies()[-1] == "稍等"
    assert [message.route for message in rig.sent] == [group, group, group, second, second]


async def test_other_conversation_can_send_while_first_conversation_types(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, replies=["第一群<n>等待的内容", "第二群"])
    await rig.say("一", route=MessageRoute("bot", "group", "one", "first"))
    await rig.say("二", route=MessageRoute("bot", "group", "two", "second"))
    assert rig.replies() == ["第一群", "第二群"]
    assert "等待的内容" not in rig.organs.plans[1].prompt


@pytest.mark.parametrize("stop", ["permission", "emergency", "maintenance", "exit", "request_stop"])
async def test_pending_parts_respect_permission_and_shutdown(
    made: list[TimelineDatabase], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    rig = make_rig(made, tmp_path, replies=["先发<n>未发送的末段"])
    await rig.say("开始")
    if stop in {"exit", "request_stop"}:
        if stop == "request_stop":
            rig.heartbeat.request_stop()
            rig.advance(10)
        await rig.heartbeat.shut_down()
        assert rig.heartbeat.seconds_until_next_beat() == 60.0
    elif stop == "permission":
        permission = rig.permissions.check

        def check(action: str) -> PermissionDecision:
            if action == ACTION_REPLY_TO_USER:
                return PermissionDecision(False, "测试撤销回复权限")
            return permission(action)

        with monkeypatch.context() as patch:
            patch.setattr(rig.permissions, "check", check)
            rig.advance(10)
        rig.advance(10)
    elif stop == "emergency":
        rig.heartbeat.halt("打字期间急停")
        rig.heartbeat.resume()
        rig.advance(10)
    else:
        rig.heartbeat.set_maintenance_paused(True)
        rig.advance(10)
        rig.heartbeat.set_maintenance_paused(False)
        rig.advance(10)
    assert rig.replies() == ["先发"]


async def test_inverted_typing_jitter_range_is_rejected() -> None:
    with pytest.raises(ValidationError, match="typing_jitter_min_seconds"):
        HeartbeatConfig(typing_jitter_min_seconds=2, typing_jitter_max_seconds=1)
