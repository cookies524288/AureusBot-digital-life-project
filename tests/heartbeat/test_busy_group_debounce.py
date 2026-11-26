"""D142：消息防抖按 steady 档等待，整批最多 8 秒，@或提及最多 1 秒。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import (
    Heartbeat,
    MediaReference,
    MessageComponent,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.conversation import _Heard
from aureus.heartbeat.inbox_attention import InboxResidence
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.heartbeat.main_loop import _debounce_shape
from aureus.heartbeat.perception import MessageRoute
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope
from tests.heartbeat.test_inbox_attention import Organs
from tests.planner_fixtures import plan_response

_IDENTITY = {
    "facts": {"name": "测试助手", "birthday": "2026-11-23"},
    "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
    "persona": {"immutable": "温和。"},
}


@pytest.mark.asyncio
async def test_busy_group_starts_planning_at_the_eight_second_batch_limit(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = Organs()
    organs.plan = '{"reply":true,"intent":"回应","stay_in_conversation":true}'
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()

    def receive(text: str, *, sender: str = "visitor") -> None:
        result = store.add(
            Perception(
                "qq",
                sender,
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER if sender == "owner" else TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", "busy", text),
            )
        )
        assert result is not None
        heart.on_inbox_message(result)
        heart.beat()

    try:
        receive("暖场", sender="owner")
        clock.advance(timedelta(seconds=4))
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert len(sent) == 1
        sent.clear()

        # 每 2 秒续发一条：活跃开批 7 秒会被不断续长，但整批最多 8 秒。
        receive("第 1 条")
        clock.advance(timedelta(seconds=2))
        receive("第 2 条")
        clock.advance(timedelta(seconds=2))
        receive("第 3 条")
        clock.advance(timedelta(seconds=2))
        receive("第 4 条")
        clock.advance(timedelta(seconds=1.9))
        heart.beat()
        assert not heart.is_thinking
        clock.advance(timedelta(seconds=0.1))
        heart.beat()
        assert heart.is_thinking

        await heart.wait_for_thought()
        heart.beat()
        assert sent and sent[-1].route is not None
        assert sent[-1].route.recipient_id == "busy"
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_busy_group_replies_to_the_latest_batch_without_chasing_the_backlog(
    tmp_path: Path,
) -> None:
    """D145：大群连发时一轮只回复最新一批，不逐条追最老，也不留下积压。"""
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = Organs()
    organs.plan = '{"reply":true,"intent":"回应","stay_in_conversation":true}'
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()

    def receive(
        text: str, *, sender: str = "visitor", message_id: str | None = None, beat: bool = True
    ) -> None:
        result = store.add(
            Perception(
                "qq",
                sender,
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER if sender == "owner" else TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", "busy", message_id or text),
            )
        )
        assert result is not None
        heart.on_inbox_message(result)
        if beat:
            heart.beat()

    try:
        receive("暖场", sender="owner")
        clock.advance(timedelta(seconds=4))
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert len(sent) == 1
        sent.clear()

        key = ConversationKey("qq", "bot", "group", "busy")
        for number in range(100):
            receive(f"第{number}条", message_id=f"busy-{number}", beat=False)
        clock.advance(timedelta(seconds=10))
        heart.beat()
        # 只读最新 15 条；更早的未读在读取时一并标已读，不逐条追最老。
        assert store.list_unread(key) == []

        await heart.wait_for_thought()
        heart.beat()
        plans = [request for request in organs.requests if request.context.get("task") == "plan"]
        assert len(plans[-1].context["in_reply_to"]) == 15
        assert all(f"第{number}条" in plans[-1].prompt for number in range(85, 100))
        assert all(f"第{number}条" not in plans[-1].prompt for number in range(0, 85))
        assert sent and sent[-1].route is not None
        assert sent[-1].route.recipient_id == "busy"
        assert len(sent) == 1
        assert store.list_unread(key) == []
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_a_mention_starts_within_one_second(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = Organs()
    organs.plan = '{"reply":true,"intent":"回应","stay_in_conversation":true}'
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()
    try:
        message = store.add(
            Perception(
                "qq",
                "visitor",
                "测试助手在吗",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", "busy", "mention"),
                mentioned=True,
            )
        )
        assert message is not None
        heart.on_inbox_message(message)
        heart.beat()
        assert not heart.is_thinking

        clock.advance(timedelta(seconds=0.9))
        heart.beat()
        assert not heart.is_thinking
        clock.advance(timedelta(seconds=0.1))
        heart.beat()
        assert heart.is_thinking

        await heart.wait_for_thought()
        heart.beat()
        assert sent and sent[-1].route is not None
        assert sent[-1].route.recipient_id == "busy"
    finally:
        await heart.shut_down()
        timeline.close()


def test_interrupt_keeps_the_restart_budget(tmp_path: Path) -> None:
    """D140：中断当前会话不能把重开计数清零，否则优先来信会反复重置上限。"""
    database = tmp_path / "aureus.db"
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=Organs(),
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    try:
        inbox = heart._platform_inbox
        assert inbox is not None
        key = ConversationKey("qq", "bot", "group", "busy")
        inbox.residence = InboxResidence(key, 35)
        heart._planner_restarts[key] = 3
        heart._interrupt_inbox(inbox, "测试抢占")
        assert inbox.residence is None
        assert heart._planner_restarts[key] == 3
    finally:
        timeline.close()


def test_group_and_private_share_the_same_debounce_mode(tmp_path: Path) -> None:
    """D142：本机 MaiBot 没有私聊独立档，群聊和私聊用同一个等待档位。"""
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=Organs(),
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    try:
        for kind, recipient in (("group", "busy"), ("private", "friend")):
            message = store.add(
                Perception(
                    "qq",
                    "visitor",
                    f"{kind} 第一条",
                    VisibilityScope.GROUP if kind == "group" else VisibilityScope.PRIVATE,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", kind, recipient, f"{kind}-one"),
                )
            )
            assert message is not None
            heart.on_inbox_message(message)
        group = heart._debouncers[ConversationKey("qq", "bot", "group", "busy")]
        private = heart._debouncers[ConversationKey("qq", "bot", "private", "friend")]
        # 两边都是新批次的安静开批，steady 等 4 秒。
        expected = clock.now().timestamp() + 4
        assert group.deadline == pytest.approx(expected)
        assert private.deadline == pytest.approx(expected)
    finally:
        timeline.close()


@pytest.mark.asyncio
async def test_steering_interrupt_limit_completes_and_delivers(tmp_path: Path) -> None:
    """D142：打断上限用尽后本轮不再打断，正常完成并投递。"""
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    plans: list[OrganRequest] = []

    class InterruptibleOrgans(Organs):
        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.context.get("task") == "plan":
                plans.append(request)
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return plan_response(
                    request, {"reply": True, "intent": "回应", "stay_in_conversation": True}
                )
            return await super().call(request)

    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=InterruptibleOrgans(),
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(
            split_reply_enabled=False,
            planner_steering_max_interrupts=1,
        ),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()

    def receive(text: str, *, sender: str = "owner") -> None:
        result = store.add(
            Perception(
                "qq",
                sender,
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER,
                route=MessageRoute("bot", "group", "busy", text),
            )
        )
        assert result is not None
        heart.on_inbox_message(result)
        heart.beat()

    async def finish_interrupt() -> None:
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        cancelled.clear()
        await heart.wait_for_thought()
        heart.beat()

    try:
        receive("第一条")
        assert not heart.is_thinking
        clock.advance(timedelta(seconds=4))
        heart.beat()
        await asyncio.wait_for(started.wait(), timeout=1)
        started.clear()

        receive("打断一")
        await finish_interrupt()
        clock.advance(timedelta(seconds=7))
        heart.beat()
        await asyncio.wait_for(started.wait(), timeout=1)
        started.clear()
        assert len(plans) == 2
        assert len(plans[-1].context["in_reply_to"]) == 2
        assert "打断一" in plans[-1].prompt

        receive("打断二")
        await asyncio.sleep(0.05)
        assert not cancelled.is_set()
        assert heart.is_thinking
        assert "打断二" not in plans[-1].prompt

        release.set()
        await heart.wait_for_thought()
        heart.beat()
        assert sent and sent[-1].route is not None
        assert sent[-1].route.recipient_id == "busy"
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("steering", "max_interrupts", "expected_stream"),
    [(True, 0, True), (False, 4, False)],
    ids=["zero-limit", "steering-off"],
)
async def test_planner_is_not_interrupted_when_zero_limit_or_steering_off(
    tmp_path: Path, steering: bool, max_interrupts: int, expected_stream: bool
) -> None:
    """D142：上限 0 表示从不打断；关闭快速反应时规划器非流式、也不打断。"""
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    plans: list[OrganRequest] = []

    class InterruptibleOrgans(Organs):
        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.context.get("task") == "plan":
                assert request.stream is expected_stream
                plans.append(request)
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return plan_response(
                    request, {"reply": True, "intent": "回应", "stay_in_conversation": True}
                )
            return await super().call(request)

    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=InterruptibleOrgans(),
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(
            split_reply_enabled=False,
            planner_message_steering=steering,
            planner_steering_max_interrupts=max_interrupts,
        ),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()
    try:
        for number, text in ((1, "第一条"), (2, "后来消息")):
            message = store.add(
                Perception(
                    "qq",
                    "owner",
                    text,
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_USER,
                    route=MessageRoute("bot", "group", "busy", str(number)),
                )
            )
            assert message is not None
            heart.on_inbox_message(message)
            heart.beat()
            if number == 1:
                clock.advance(timedelta(seconds=4))
                heart.beat()
                await asyncio.wait_for(started.wait(), timeout=1)
                started.clear()
        await asyncio.sleep(0.05)
        assert not cancelled.is_set()
        assert heart.is_thinking
        assert len(plans) == 1

        release.set()
        await heart.wait_for_thought()
        heart.beat()
        assert sent and sent[-1].route is not None
        assert sent[-1].route.recipient_id == "busy"
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_off_mode_still_interrupts_with_quick_reaction(tmp_path: Path) -> None:
    """D142：关闭防抖时不等待，但快速反应仍可打断规划并带上新消息重来。"""
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    plans: list[OrganRequest] = []

    class InterruptibleOrgans(Organs):
        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.context.get("task") == "plan":
                plans.append(request)
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return plan_response(
                    request, {"reply": True, "intent": "回应", "stay_in_conversation": True}
                )
            return await super().call(request)

    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=InterruptibleOrgans(),
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(
            message_debounce_mode="off",
            split_reply_enabled=False,
        ),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()

    def receive(number: int, text: str) -> None:
        message = store.add(
            Perception(
                "qq",
                "owner",
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER,
                route=MessageRoute("bot", "group", "busy", str(number)),
            )
        )
        assert message is not None
        heart.on_inbox_message(message)
        heart.beat()

    try:
        receive(1, "第一条")
        await asyncio.wait_for(started.wait(), timeout=1)
        started.clear()
        assert heart._debouncers == {}

        receive(2, "打断")
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await heart.wait_for_thought()
        heart.beat()
        await asyncio.wait_for(started.wait(), timeout=1)
        started.clear()
        assert len(plans) == 2
        assert plans[-1].stream is True
        assert len(plans[-1].context["in_reply_to"]) == 2
        assert "打断" in plans[-1].prompt

        release.set()
        await heart.wait_for_thought()
        heart.beat()
        assert sent and sent[-1].route is not None
        assert sent[-1].route.recipient_id == "busy"
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_emergency_stop_cancel_keeps_the_restart_budget(tmp_path: Path) -> None:
    """D140：急停取消进行中的规划不重置重开上限。"""
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    started = asyncio.Event()

    class HangingOrgans(Organs):
        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.context.get("task") == "plan":
                self.requests.append(request)
                started.set()
                await asyncio.Future()
            return await super().call(request)

    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=HangingOrgans(),
        identity=Identity.model_validate(_IDENTITY),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    heart.attach_channel("qq", lambda _message: None)
    heart.wake_up()
    key = ConversationKey("qq", "bot", "group", "busy")
    try:
        message = store.add(
            Perception(
                "qq",
                "owner",
                "暖场",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER,
                route=MessageRoute("bot", "group", "busy", "one"),
            )
        )
        assert message is not None
        heart.on_inbox_message(message)
        heart.beat()
        heart._planner_restarts[key] = 2
        clock.advance(timedelta(seconds=4))
        heart.beat()
        await asyncio.wait_for(started.wait(), timeout=2)

        heart.halt("测试急停")
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert heart._planner_restarts[key] == 2
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.parametrize(
    ("perception", "expected"),
    [
        (
            Perception(
                "qq",
                "visitor",
                "你好",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                components=(MessageComponent("text", 0, text="你好"),),
            ),
            (False, False),
        ),
        (
            Perception(
                "qq",
                "visitor",
                "[图片]",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                attachments=(MediaReference("image", "photo"),),
                components=(MessageComponent("image", 0, attachment_index=0),),
            ),
            (True, False),
        ),
        (
            Perception(
                "qq",
                "visitor",
                "[表情]",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                attachments=(MediaReference("image", "emoji", True),),
                components=(MessageComponent("image", 0, attachment_index=0),),
            ),
            (False, True),
        ),
        (
            Perception(
                "qq",
                "visitor",
                "[图片] [表情]",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                attachments=(
                    MediaReference("image", "photo"),
                    MediaReference("image", "emoji", True),
                ),
                components=(
                    MessageComponent("image", 0, attachment_index=0),
                    MessageComponent("image", 1, attachment_index=1),
                ),
            ),
            (True, False),
        ),
        (
            Perception(
                "qq",
                "visitor",
                "看看 [图片]",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                attachments=(MediaReference("image", "photo"),),
                components=(
                    MessageComponent("text", 0, text="看看 "),
                    MessageComponent("image", 1, attachment_index=0),
                ),
            ),
            (False, False),
        ),
    ],
)
def test_message_shape_detects_bare_images_and_pure_emoji(
    perception: Perception, expected: tuple[bool, bool]
) -> None:
    assert _debounce_shape(perception) == expected


def test_off_mode_never_records_a_deadline(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=Organs(),
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {"name": "维护者"},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(message_debounce_mode="off"),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
    )
    perception = Perception(
        "qq",
        "visitor",
        "在吗",
        VisibilityScope.GROUP,
        TrustLevel.VERIFIED_CONTACT,
        route=MessageRoute("bot", "group", "busy", "one"),
    )
    message = InboxMessage(
        id=1,
        conversation=ConversationKey("qq", "bot", "group", "busy"),
        perception=perception,
        sender_name="",
        conversation_name="",
        timestamp=clock.now(),
        status="unread",
    )
    try:
        heart._observe_debounce(message)
        assert heart._debouncers == {}
        record = timeline.append(
            "message",
            "在吗",
            ActorType.PERSON,
            metadata={"inbox_message_id": message.id},
        )
        assert heart._debounce_remaining([_Heard(perception, record, False)]) == 0.0
    finally:
        timeline.close()
