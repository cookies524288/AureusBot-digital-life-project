"""回复拆成几条短消息发出的流程测试（路线图 1.7，D60）。

发给用户的是几条短消息，时间线里仍是完整的一条；关掉拆分或限制条数时按配置来。
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import (
    EVENT_MESSAGE,
    Heartbeat,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

pytestmark = pytest.mark.asyncio

CHANNEL = "terminal"
REPLY = "今天面试被拒了。别难过！我陪你聊聊？"
PIECES = ["今天面试被拒了", "别难过！", "我陪你聊聊？"]


class FixedOrgans:
    """每次都返回同一段回复的假器官。"""

    async def call(self, request: OrganRequest) -> OrganResponse:
        return OrganResponse(organ_id=request.organ_id, suggestion=REPLY)


class Rig:
    def __init__(self, timeline: TimelineDatabase, heartbeat: Heartbeat) -> None:
        self.timeline = timeline
        self.heartbeat = heartbeat
        self.sent: list[OutgoingMessage] = []
        heartbeat.attach_channel(CHANNEL, self.sent.append)
        heartbeat.wake_up()

    async def say(self, content: str) -> None:
        assert self.heartbeat.submit(
            Perception(
                channel=CHANNEL,
                sender_id="local",
                content=content,
                visibility=VisibilityScope.USER_ONLY,
                trust=TrustLevel.VERIFIED_USER,
            )
        )
        self.heartbeat.beat()
        await self.heartbeat.wait_for_thought()
        self.heartbeat.beat()

    def replies(self) -> list[str]:
        return [m.content for m in self.sent if m.kind is MessageKind.REPLY]


@pytest.fixture
def made(tmp_path: Path) -> Iterator[list[TimelineDatabase]]:
    timelines: list[TimelineDatabase] = []
    yield timelines
    for timeline in timelines:
        timeline.close()


def make_rig(made: list[TimelineDatabase], tmp_path: Path, **config: Any) -> Rig:
    clock = ManualClock(datetime(2026, 11, 17, 12, 0, tzinfo=UTC))
    timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
    timeline.open()
    made.append(timeline)
    emergency_stop = EmergencyStop(tmp_path / "halt.json", clock)
    heartbeat = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=FixedOrgans(),
        identity=Identity.model_validate(
            {
                "facts": {"name": "小绿", "birthday": "2008-01-01"},
                "user": {"name": "阿明"},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(planning_enabled=False, **config),
        permissions=PermissionChecker(emergency_stop),
        emergency_stop=emergency_stop,
        timezone=UTC,
    )
    return Rig(timeline, heartbeat)


async def test_a_reply_is_sent_as_several_short_messages(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    await rig.say("我今天不太顺")

    assert rig.replies() == PIECES
    assert all(m.sender_name == "小绿" for m in rig.sent)


async def test_the_timeline_keeps_the_whole_reply_as_one_record(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    await rig.say("我今天不太顺")

    records = rig.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=100))
    mine = [r for r in records if r.actor_type is ActorType.ME]
    assert [r.content for r in mine] == [REPLY]
    assert [message.timeline_id for message in rig.sent] == [mine[0].id] * len(PIECES)
    assert all(message.timestamp == mine[0].timestamp for message in rig.sent)


async def test_switching_splitting_off_sends_the_whole_reply(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, split_reply_enabled=False)
    await rig.say("我今天不太顺")

    assert rig.replies() == [REPLY]


async def test_the_message_limit_is_respected(made: list[TimelineDatabase], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, max_reply_messages=2)
    await rig.say("我今天不太顺")

    assert len(rig.replies()) == 2
    assert "".join(rig.replies()).count("别难过") == 1


async def test_a_limit_of_one_sends_one_message(
    made: list[TimelineDatabase], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, max_reply_messages=1)
    await rig.say("我今天不太顺")

    assert rig.replies() == [REPLY]
