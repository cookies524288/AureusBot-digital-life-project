"""D94：用假时钟和假器官检查 QQ 收件箱进入心跳的时机与回投。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, ManualClock
from aureus.heartbeat import (
    Heartbeat,
    MediaReference,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.inbox_attention import InboxAttention, InboxSelection, select_conversations
from aureus.heartbeat.inbox_store import InboxStore
from aureus.heartbeat.perception import MessageRoute
from aureus.identity import Identity
from aureus.organs import ModelPreference, OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase, TimelineQuery, VisibilityScope


class Organs:
    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []
        self.plan = '{"reply":true,"intent":"回应"}'

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if request.context.get("task") == "inbox_select":
            return OrganResponse(request.organ_id, '{"conversations":[1,2,3]}')
        suggestion = {"planner": self.plan, "vlm": "识别出的内容"}.get(request.organ_id, "收到。")
        return OrganResponse(organ_id=request.organ_id, suggestion=suggestion)


def test_idle_browse_orders_conversations_by_oldest_unread(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    store = InboxStore(database, clock)
    attention = InboxAttention(
        store, TimelineDatabase(database, clock), None, [], enabled=True, read_limit=35
    )

    def save(group: str, message: str) -> None:
        assert (
            store.add(
                Perception(
                    "qq",
                    "visitor",
                    message,
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", "group", group, message),
                )
            )
            is not None
        )

    save("busy", "oldest")
    for number in range(45):
        save("backlog", str(number))
    save("busy", "newest")
    assert attention.browse(clock.now(), can_browse=True) == []
    clock.advance(timedelta(minutes=5))
    candidates = attention.browse(clock.now(), can_browse=True)
    rows = attention.read_selection(InboxSelection(tuple(item.key for item in candidates)))
    assert [row.perception.content for row in rows[:2]] == ["oldest", "newest"]
    assert len(rows) == 35


@pytest.mark.asyncio
async def test_planner_selects_which_private_and_multiple_groups(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    store = InboxStore(database, clock)
    attention = InboxAttention(
        store, TimelineDatabase(database, clock), None, [], enabled=True, read_limit=4
    )
    for number, (kind, name) in enumerate(
        [
            ("group", "denied"),
            ("group", "group-one"),
            ("private", "private-first"),
            ("private", "private-second"),
            ("group", "group-two"),
            ("group", "group-two"),
        ],
        1,
    ):
        assert (
            store.add(
                Perception(
                    "qq",
                    "sender",
                    f"message-{number}",
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", kind, name, str(number)),
                ),
                conversation_name=name,
            )
            is not None
        )
    attention.allows = lambda key: key.recipient_id != "denied"
    attention.browse(clock.now(), can_browse=True)
    clock.advance(timedelta(minutes=5))
    candidates = attention.browse(clock.now(), can_browse=True)

    async def choose(request: OrganRequest) -> OrganResponse:
        assert "denied" not in request.prompt
        assert "message-" not in request.prompt
        return OrganResponse(request.organ_id, '{"conversations":[4,2,3,1,4]}')

    selection = await select_conversations(
        candidates, choose, "planner", ModelPreference.PREFERRED, stream=False
    )
    rows = attention.read_selection(selection)
    assert [row.perception.content for row in rows] == [
        "message-4",
        "message-5",
        "message-6",
        "message-2",
    ]


def test_disallowed_priority_unread_does_not_hide_allowed_mail(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    store = InboxStore(database, clock)
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    try:
        attention = InboxAttention(
            store, timeline, None, [("qq", "owner")], enabled=True, read_limit=35
        )
        attention.allows = lambda conversation: conversation.recipient_id == "current"
        for number in range(26):
            route = MessageRoute(
                "bot", "group", "former" if number < 25 else "current", str(number)
            )
            assert (
                store.add(
                    Perception(
                        "qq",
                        "owner",
                        str(number),
                        VisibilityScope.GROUP,
                        TrustLevel.VERIFIED_CONTACT,
                        route=route,
                    )
                )
                is not None
            )
        assert [row.perception.content for row in attention.priority_messages()] == ["25"]
        assert sum(row.unread_count for row in store.list_conversations()) == 26
    finally:
        timeline.close()


@pytest.mark.asyncio
async def test_active_conversation_debounces_and_limits_interruptions(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    started = asyncio.Event()
    release = asyncio.Event()
    plans: list[OrganRequest] = []
    replies: list[OrganRequest] = []
    cancellations: list[int] = []

    class StreamingOrgans:
        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.organ_id == "planner":
                assert request.stream
                plans.append(request)
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellations.append(len(plans))
                    raise
                if "离开当前群" in request.prompt:
                    return OrganResponse("planner", '{"reply":false,"stay_in_conversation":false}')
                return OrganResponse(
                    "planner", '{"reply":true,"intent":"回应","stay_in_conversation":true}'
                )
            assert not request.stream
            replies.append(request)
            return OrganResponse("replyer", "收到。")

    identity = Identity.model_validate(
        {
            "facts": {"name": "小豆", "birthday": "2026-11-23"},
            "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
            "persona": {"immutable": "温和。"},
        }
    )
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=StreamingOrgans(),
        identity=identity,
        config=HeartbeatConfig(inbox_debounce_enabled=True, split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent: list[OutgoingMessage] = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()

    def receive(message_id: str, *, sender: str = "visitor", group: str = "ongoing") -> None:
        result = store.add(
            Perception(
                "qq",
                sender,
                message_id,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", group, message_id),
            ),
        )
        assert result is not None
        heart.on_inbox_message(result)
        heart.beat()

    async def advance_to_planner() -> None:
        clock.advance(timedelta(seconds=4))
        heart.beat()
        await asyncio.wait_for(started.wait(), timeout=1)
        started.clear()

    async def finish_plan() -> None:
        release.set()
        await heart.wait_for_thought()
        heart.beat()
        release.clear()

    try:
        receive("first-alert", sender="owner")
        clock.advance(timedelta(seconds=3))
        receive("second-window")
        assert not plans and heart.seconds_until_next_beat() == 4
        await advance_to_planner()
        assert all(text in plans[0].prompt for text in ("first-alert", "second-window"))
        await finish_plan()

        receive("ongoing-message")
        await advance_to_planner()
        for number in range(4):
            receive(f"interrupt-{number}")
            await heart.wait_for_thought()
            heart.beat()
            assert len(cancellations) == number + 1
            await advance_to_planner()
            assert f"interrupt-{number}" in plans[-1].prompt
        receive("exceeds-limit")
        assert len(cancellations) == 4
        assert "exceeds-limit" not in plans[-1].prompt
        await finish_plan()
        await advance_to_planner()
        assert "exceeds-limit" in plans[-1].prompt
        await finish_plan()

        receive("priority-elsewhere", sender="owner", group="priority-group")
        await advance_to_planner()
        assert "priority-elsewhere" in plans[-1].prompt
        await finish_plan()
        receive("离开当前群", group="priority-group")
        await advance_to_planner()
        await finish_plan()
        assert len(replies) == 4
        assert all(item.route is not None for item in sent if item.kind is MessageKind.REPLY)
        receive("ordinary-after-leaving", group="priority-group")
        assert not heart.is_thinking
        assert store.list_unread_conversations()[0].unread_count == 1
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_read_unread_when_idle_and_wake_for_special_contact(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    people = PeopleDirectory(database, clock)
    people.open()
    calendar = BudgetCalendar(UTC)
    ledger = Ledger(database, clock, calendar=calendar)
    ledger.open()
    try:
        people.ensure_user("主人", [])
        store = InboxStore(database, clock)
        stop = EmergencyStop(tmp_path / "halt.json", clock)
        organs = Organs()
        heart = Heartbeat(
            clock=clock,
            timeline=timeline,
            organs=organs,
            identity=Identity.model_validate(
                {
                    "facts": {"name": "小豆", "birthday": "2026-11-23"},
                    "user": {"name": "主人"},
                    "persona": {"immutable": "温和。"},
                }
            ),
            config=HeartbeatConfig(split_reply_enabled=False, inbox_debounce_enabled=False),
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            people=people,
            inbox_store=store,
            body=Body(BodyConfig(), None, ledger, clock, calendar),
            timezone=UTC,
        )
        sent: list[OutgoingMessage] = []
        heart.attach_channel("qq", sent.append)
        heart.update_available_organs(frozenset({"vlm"}))
        picture = MediaReference("image", "group.png")
        reads: list[tuple[Perception, MediaReference]] = []

        async def read_attachment(
            source: Perception, attachment: MediaReference
        ) -> tuple[bytes, str]:
            reads.append((source, attachment))
            return b"test-image", "image/png"

        heart.set_inbox_attachment_reader(read_attachment)
        heart.wake_up()

        def receive(
            sender: str,
            message_id: str,
            *,
            private: bool = False,
            attachments: tuple[MediaReference, ...] = (),
        ) -> Perception:
            route = MessageRoute(
                "bot", "private" if private else "group", sender if private else "group", message_id
            )
            perception = Perception(
                "qq",
                sender,
                f"消息 {message_id}",
                VisibilityScope.USER_ONLY if private else VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER if private else TrustLevel.VERIFIED_CONTACT,
                route=route,
                attachments=attachments,
            )
            message = store.add(perception)
            assert message is not None
            heart.on_inbox_message(message)
            return perception

        people.ensure_user("主人", [("qq", "owner")])
        owner = receive("owner", "private-only", private=True)
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert sent[-1].route == owner.route
        organs.plan = '{"reply":true,"intent":"回应","special_contact":true}'
        group = receive("visitor", "group-only", attachments=(picture,))
        heart.beat()
        clock.advance(timedelta(minutes=4))
        heart.beat()
        assert len(sent) == 1
        assert reads == []
        clock.advance(timedelta(minutes=1))
        heart.beat()
        assert heart.is_thinking
        await heart.wait_for_thought()
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert sent[-1].route == group.route
        assert reads == [(group, picture)]
        assert "识别出的内容" in organs.requests[-2].prompt
        assert "private-only" not in organs.requests[-2].prompt
        assert store.add(group) is None
        changes = timeline.query(TimelineQuery(event_type="special_contact_changed"))
        assert len(changes) == 1
        assert changes[0].metadata["add"] is True

        clock.advance(timedelta(hours=17))
        ordinary = receive("other-visitor", "night-ordinary")
        heart.beat()
        assert not heart.is_thinking
        assert heart.seconds_until_next_beat() > 60
        priority = receive("visitor", "priority")
        heart.beat()
        assert heart.is_thinking
        await heart.wait_for_thought()
        heart.beat()
        assert sent[-1].route == priority.route
        assert sum(row.unread_count for row in store.list_conversations()) == 2
        assert sum(item.route == priority.route for item in sent) == 1
        assert ordinary.route is not None
        assert not any(item.route == ordinary.route for item in sent)

        heart.set_maintenance_paused(True)
        buffered = receive("owner", "maintenance-buffered", private=True)
        heart.beat()
        assert heart.maintenance_paused and not heart.is_thinking
        assert not any(item.route == buffered.route for item in sent)
        assert sum(row.unread_count for row in store.list_conversations()) == 3

        heart.set_maintenance_paused(False)
        heart.beat()
        await heart.wait_for_thought()
        heart.set_maintenance_paused(True)
        heart.beat()
        assert not any(item.route == buffered.route for item in sent)
        heart.set_maintenance_paused(False)
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert sent[-1].route == buffered.route
        await heart.shut_down()
    finally:
        people.close()
        ledger.close()
        timeline.close()
