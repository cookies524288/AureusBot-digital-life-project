"""网页实时事件的发布测试（3.43）：事件总线、心跳发布和急停/停机暂停。

用手动时钟、真的时间线数据库和假器官，不联网、不花钱。
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from aureus.channels import WEB_CHANNEL_NAME, WEB_USER_ID
from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import Heartbeat, MessageRoute, Perception, TrustLevel, VisibilityScope
from aureus.heartbeat.inbox_store import InboxStore
from aureus.heartbeat.language_runner import LanguageKind, TrimmedLearner
from aureus.heartbeat.live import (
    RESOURCE_EXPRESSION,
    RESOURCE_LANGUAGE,
    TOPIC_INBOX,
    TOPIC_RESOURCES,
    TOPIC_ROUND,
    LiveEvent,
    LiveEventHub,
)
from aureus.heartbeat.pending_learning_buffer import PendingLearningBuffer
from aureus.identity import Identity
from aureus.memory import LanguageSample
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import TimelineDatabase
from tests.planner_fixtures import plan_response

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
CHINA = timezone(timedelta(hours=8))


def make_identity() -> Identity:
    return Identity.model_validate(
        {
            "facts": {"name": "小绿", "birthday": "2008-01-01"},
            "user": {"name": "阿明"},
            "persona": {"immutable": "温和，好奇。", "expression_style": "句子短。"},
        }
    )


class FakeOrgans:
    """假的器官调用：规划器建议回复，回复器给一句短话。"""

    async def call(self, request: OrganRequest) -> OrganResponse:
        if request.context.get("task") == "plan":
            return plan_response(request, {"reply": True, "intent": "回应"})
        return OrganResponse(organ_id=request.organ_id, suggestion="嗯。")


def web_said(content: str) -> Perception:
    return Perception(
        channel=WEB_CHANNEL_NAME,
        sender_id=WEB_USER_ID,
        content=content,
        visibility=VisibilityScope.USER_ONLY,
        trust=TrustLevel.VERIFIED_USER,
    )


def inbox_perception(content: str = "在吗") -> Perception:
    return Perception(
        channel="qq",
        sender_id="10001",
        content=content,
        visibility=VisibilityScope.USER_ONLY,
        trust=TrustLevel.VERIFIED_USER,
        route=MessageRoute(
            account_id="999",
            kind="private",
            recipient_id="10001",
            message_id="m-1",
        ),
    )


class Rig:
    def __init__(self, tmp_path: Path, *, with_inbox: bool = False) -> None:
        self.clock = ManualClock(START)
        self.timeline = TimelineDatabase(tmp_path / "aureus.db", self.clock)
        self.timeline.open()
        self.emergency_stop = EmergencyStop(tmp_path / "halt.json", self.clock)
        self.inbox = InboxStore(tmp_path / "aureus.db", self.clock) if with_inbox else None
        self.events: list[LiveEvent] = []
        self.heartbeat = Heartbeat(
            clock=self.clock,
            timeline=self.timeline,
            organs=FakeOrgans(),
            identity=make_identity(),
            config=HeartbeatConfig(split_reply_enabled=False),
            permissions=PermissionChecker(self.emergency_stop),
            emergency_stop=self.emergency_stop,
            timezone=CHINA,
            inbox_store=self.inbox,
        )
        self.heartbeat.live_events.subscribe(self.events.append)

    async def start(self) -> None:
        """醒来并跳第一拍：这时才读到急停状态，开始推送。"""
        self.heartbeat.wake_up()
        await self.heartbeat.beat()
        self.events.clear()

    def close(self) -> None:
        self.timeline.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


@pytest.fixture
def inbox_rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path, with_inbox=True)
    yield made
    made.close()


# ---------- 事件总线 ----------


async def test_the_hub_delivers_to_every_subscriber() -> None:
    hub = LiveEventHub()
    first: list[LiveEvent] = []
    second: list[LiveEvent] = []
    hub.subscribe(first.append)
    hub.subscribe(second.append)
    event = LiveEvent("timeline", {"id": 3})

    hub.publish(event)

    assert first == [event]
    assert second == [event]


async def test_unsubscribing_stops_delivery_and_can_repeat() -> None:
    hub = LiveEventHub()
    received: list[LiveEvent] = []
    unsubscribe = hub.subscribe(received.append)
    unsubscribe()
    unsubscribe()

    hub.publish(LiveEvent("timeline", {"id": 1}))

    assert received == []


async def test_a_failing_subscriber_does_not_stop_the_others() -> None:
    hub = LiveEventHub()
    received: list[LiveEvent] = []

    def broken(_event: LiveEvent) -> None:
        raise RuntimeError("这个网页的队列坏了")

    hub.subscribe(broken)
    hub.subscribe(received.append)

    hub.publish(LiveEvent("timeline", {"id": 2}))

    assert received == [LiveEvent("timeline", {"id": 2})]


async def test_a_paused_hub_drops_events_until_it_resumes() -> None:
    hub = LiveEventHub()
    received: list[LiveEvent] = []
    hub.subscribe(received.append)

    hub.pause()
    hub.publish(LiveEvent("timeline", {"id": 1}))
    assert received == []

    hub.resume()
    hub.publish(LiveEvent("timeline", {"id": 2}))
    assert received == [LiveEvent("timeline", {"id": 2})]


# ---------- 心跳发布 ----------


async def test_a_saved_platform_message_publishes_only_its_inbox_id(inbox_rig: Rig) -> None:
    await inbox_rig.start()
    assert inbox_rig.inbox is not None
    message = inbox_rig.inbox.add(inbox_perception("私聊正文"), sender_name="对方")
    assert message is not None

    inbox_rig.heartbeat.on_inbox_message(message)

    assert inbox_rig.events == [LiveEvent(TOPIC_INBOX, {"id": message.id})]
    assert "私聊正文" not in str(inbox_rig.events)


async def test_a_web_reply_publishes_active_and_inactive_round_states(rig: Rig) -> None:
    await rig.start()
    rig.heartbeat.attach_channel(WEB_CHANNEL_NAME, lambda _message: None)
    assert rig.heartbeat.submit(web_said("在吗"))

    await rig.heartbeat.beat()
    assert rig.events[-1] == LiveEvent(TOPIC_ROUND, {"active": True, "web": True})

    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()
    assert rig.events[-1] == LiveEvent(TOPIC_ROUND, {"active": False})


async def test_emergency_stop_pauses_publishing_and_resume_restores_it(
    inbox_rig: Rig,
) -> None:
    await inbox_rig.start()
    assert inbox_rig.inbox is not None
    message = inbox_rig.inbox.add(inbox_perception(), sender_name="对方")
    assert message is not None

    inbox_rig.heartbeat.halt("测试急停")
    assert inbox_rig.heartbeat.live_events.paused
    inbox_rig.heartbeat.on_inbox_message(message)
    assert inbox_rig.events == []

    inbox_rig.heartbeat.resume()
    assert not inbox_rig.heartbeat.live_events.paused
    inbox_rig.heartbeat.on_inbox_message(message)
    assert inbox_rig.events == [LiveEvent(TOPIC_INBOX, {"id": message.id})]


async def test_a_halt_from_another_process_is_noticed_on_the_next_beat(
    inbox_rig: Rig,
) -> None:
    await inbox_rig.start()
    assert inbox_rig.inbox is not None
    message = inbox_rig.inbox.add(inbox_perception(), sender_name="对方")
    assert message is not None
    inbox_rig.emergency_stop.engage("命令行急停")

    await inbox_rig.heartbeat.beat()

    assert inbox_rig.heartbeat.live_events.paused
    inbox_rig.heartbeat.on_inbox_message(message)
    assert inbox_rig.events == []


async def test_maintenance_pause_and_shutdown_stop_publishing(rig: Rig) -> None:
    await rig.start()

    rig.heartbeat.set_maintenance_paused(True)
    assert rig.heartbeat.live_events.paused
    rig.heartbeat.set_maintenance_paused(False)
    assert not rig.heartbeat.live_events.paused

    rig.heartbeat.request_stop()
    assert rig.heartbeat.live_events.paused


async def test_a_finished_language_batch_publishes_a_language_resource(rig: Rig) -> None:
    await rig.start()
    learner = TrimmedLearner(
        LanguageKind.EXPRESSION,
        "web",
        PendingLearningBuffer[LanguageSample](lambda sample: sample.source_key),
    )
    task = asyncio.create_task(asyncio.sleep(0, result=True))
    await task

    rig.heartbeat._finish_language_task(task, learner, ())

    assert LiveEvent(TOPIC_RESOURCES, {"resource": RESOURCE_LANGUAGE}) in rig.events


async def test_an_index_batch_publishes_its_resource(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.start()

    async def build() -> tuple[bool, bool]:
        return False, True

    monkeypatch.setattr(rig.heartbeat, "_index_builders", lambda: [("expression", build)])

    await rig.heartbeat._build_indexes()

    assert LiveEvent(TOPIC_RESOURCES, {"resource": RESOURCE_EXPRESSION}) in rig.events
