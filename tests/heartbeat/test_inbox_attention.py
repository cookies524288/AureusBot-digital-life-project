"""D94：用假时钟和假器官检查 QQ 收件箱进入心跳的时机与回投。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, ManualClock
from aureus.heartbeat import Heartbeat, MediaReference, OutgoingMessage, Perception, TrustLevel
from aureus.heartbeat.inbox_attention import InboxAttention
from aureus.heartbeat.inbox_store import InboxStore
from aureus.heartbeat.perception import MessageRoute
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase, TimelineQuery, VisibilityScope


class Organs:
    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []
        self.plan = '{"reply":true,"intent":"回应"}'

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
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
    rows = attention.browse(clock.now(), can_browse=True)
    assert [row.perception.content for row in rows[:2]] == ["oldest", "newest"]
    assert len(rows) == 35


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
            config=HeartbeatConfig(split_reply_enabled=False),
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
