"""D94：用假时钟和假器官检查 QQ 收件箱进入心跳的时机与回投。"""

import asyncio
import sqlite3
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
from aureus.heartbeat.events import (
    EVENT_PLANNING_INTERRUPTED,
    EVENT_REPLY_DELIVERED,
    EVENT_REPLY_DELIVERY_FAILED,
    EVENT_REPLY_REFERENCE,
)
from aureus.heartbeat.inbox_attention import InboxAttention, InboxSelection, select_conversations
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.heartbeat.perception import MessageRoute
from aureus.heartbeat.planning import Plan
from aureus.identity import Identity
from aureus.organs import ModelPreference, OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope
from tests.planner_fixtures import plan_response
from tests.prompt_assertions import dialogue_text


class Organs:
    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []
        self.plan = '{"reply":true,"intent":"回应"}'

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if request.context.get("task") == "inbox_select":
            return OrganResponse(request.organ_id, '{"conversation":1,"count":1}')
        if request.context.get("task") == "plan":
            return plan_response(request, self.plan)
        suggestion = {"vlm": "识别出的内容"}.get(request.organ_id, "收到。")
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
    assert [item.key.recipient_id for item in candidates] == ["busy", "backlog"]
    rows = attention.read_selection(InboxSelection(candidates[1].key, 35))
    # D145：只读最新的 35 条，更早的未读一并标已读丢弃，返回仍按时间升序。
    assert rows[0].perception.content == "10"
    assert rows[-1].perception.content == "44"
    assert len(rows) == 35
    assert attention.active_conversation == candidates[1].key
    assert store.list_unread(candidates[1].key) == []
    assert len(store.list_unread(candidates[0].key)) == 2


@pytest.mark.asyncio
async def test_planner_selects_one_group_and_rechecks_scope(tmp_path: Path) -> None:
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
        return OrganResponse(request.organ_id, '{"conversation":4,"count":1}')

    selection = await select_conversations(
        candidates, choose, "planner", ModelPreference.PREFERRED, read_limit=4, stream=False
    )
    assert selection.conversation == candidates[3].key
    assert selection.conversation is not None
    attention.allows = lambda key: key != selection.conversation
    assert attention.read_selection(selection) == []
    assert len(store.list_unread(selection.conversation)) == 2
    attention.allows = lambda key: key.account_id == "bot" and key.recipient_id == "group-two"
    rows = attention.read_selection(selection)
    # D145：count=1 表示读最新 1 条，同会话更早的未读一起清掉。
    assert [row.perception.content for row in rows] == ["message-6"]
    assert store.list_unread(selection.conversation) == []
    assert len(store.list_unread(candidates[1].key)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suggestion",
    [
        '{"conversation":null}',
        '{"conversation":true,"count":1}',
        '{"conversation":1,"count":true}',
        '{"conversation":2,"count":1}',
        '{"conversation":1,"count":0}',
        '{"conversation":1,"count":5}',
        '{"conversations":[1]}',
        '{"conversation":[1],"count":1}',
        '{"conversation":1, "conversation":1, "count":1}',
        '{"conversation":1,"count":1} {"conversation":1,"count":3}',
    ],
)
async def test_planner_invalid_or_declined_selection_does_not_enter(
    tmp_path: Path, suggestion: str
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    store = InboxStore(tmp_path / "aureus.db", clock)
    row = store.add(
        Perception(
            "qq",
            "visitor",
            "未读",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", "group-one", "message"),
        )
    )
    assert row is not None
    attention = InboxAttention(
        store, TimelineDatabase(tmp_path / "aureus.db", clock), None, [], enabled=True, read_limit=4
    )

    async def choose(request: OrganRequest) -> OrganResponse:
        return OrganResponse(request.organ_id, suggestion)

    result = await select_conversations(
        store.list_unread_conversations(),
        choose,
        "planner",
        ModelPreference.PREFERRED,
        read_limit=4,
        stream=False,
    )
    assert attention.read_selection(result) == []
    assert attention.active_conversation is None
    assert store.list_unread(row.conversation) == [row]


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


def test_active_conversation_reads_all_new_unread_not_entry_count(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    store = InboxStore(database, clock)
    attention = InboxAttention(
        store, TimelineDatabase(database, clock), None, [], enabled=True, read_limit=5
    )

    def save(message: str) -> None:
        assert (
            store.add(
                Perception(
                    "qq",
                    "visitor",
                    message,
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", "group", "active", message),
                )
            )
            is not None
        )

    save("first")
    key = ConversationKey("qq", "bot", "group", "active")
    assert [row.perception.content for row in attention.read_selection(InboxSelection(key, 1))] == [
        "first"
    ]
    assert attention.residence is not None and attention.residence.read_limit == 1
    for message in ("second", "third", "fourth"):
        save(message)
    assert [row.perception.content for row in attention.active_messages()] == [
        "second",
        "third",
        "fourth",
    ]
    assert store.list_unread(key) == []


def test_busy_group_reads_only_the_latest_limit_and_clears_the_backlog(tmp_path: Path) -> None:
    """D145：积压 100 条时一轮只读最新 15 条，未读清零；下一轮只看到新到的。"""
    clock = ManualClock(datetime(2026, 11, 26, tzinfo=UTC))
    store = InboxStore(tmp_path / "aureus.db", clock)
    attention = InboxAttention(
        store,
        TimelineDatabase(tmp_path / "aureus.db", clock),
        None,
        [],
        enabled=True,
        read_limit=15,
    )

    def save(message: str) -> None:
        assert (
            store.add(
                Perception(
                    "qq",
                    "visitor",
                    message,
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", "group", "busy", message),
                )
            )
            is not None
        )

    key = ConversationKey("qq", "bot", "group", "busy")
    for number in range(100):
        save(str(number))
    rows = attention.read_selection(InboxSelection(key, 15))
    assert [row.perception.content for row in rows] == [str(number) for number in range(85, 100)]
    assert store.list_unread(key) == []

    save("new-1")
    save("new-2")
    assert [row.perception.content for row in attention.active_messages()] == ["new-1", "new-2"]
    assert store.list_unread(key) == []


def test_debounce_messages_reads_latest_limit_and_clears(tmp_path: Path) -> None:
    """D145：防抖补读只取最新一批，同会话更早未读一并清掉。"""
    clock = ManualClock(datetime(2026, 11, 26, tzinfo=UTC))
    store = InboxStore(tmp_path / "aureus.db", clock)
    attention = InboxAttention(
        store,
        TimelineDatabase(tmp_path / "aureus.db", clock),
        None,
        [],
        enabled=True,
        read_limit=15,
    )

    def save(number: int) -> None:
        assert (
            store.add(
                Perception(
                    "qq",
                    "visitor",
                    str(number),
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", "group", "busy", str(number)),
                )
            )
            is not None
        )

    key = ConversationKey("qq", "bot", "group", "busy")
    for number in range(40):
        save(number)
    rows = attention.debounce_messages(key)
    assert [row.perception.content for row in rows] == [str(number) for number in range(25, 40)]
    assert store.list_unread(key) == []


def test_priority_path_submits_priority_and_clears_same_conversation_backlog(
    tmp_path: Path,
) -> None:
    """D145：优先来信单独回复；同会话更早的未读提交后一并清掉，不堆积。"""
    clock = ManualClock(datetime(2026, 11, 26, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=Organs(),
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    heart.attach_channel("qq", lambda _message: None)
    heart.wake_up()
    try:
        key = ConversationKey("qq", "bot", "group", "busy")
        for number in range(20):
            assert (
                store.add(
                    Perception(
                        "qq",
                        "visitor",
                        f"第 {number} 条",
                        VisibilityScope.GROUP,
                        TrustLevel.VERIFIED_CONTACT,
                        route=MessageRoute("bot", "group", "busy", f"visitor-{number}"),
                    )
                )
                is not None
            )
        assert (
            store.add(
                Perception(
                    "qq",
                    "owner",
                    "紧急",
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_USER,
                    route=MessageRoute("bot", "group", "busy", "owner"),
                )
            )
            is not None
        )
        inbox = heart._platform_inbox
        assert inbox is not None
        heart._take_inbox_priority(inbox)
        submitted = [perception.content for perception, _ in heart._pending]
        assert submitted == ["紧急"]
        assert store.list_unread(key) == []
        heart._take_in_perceptions()
        assert store.list_unread(key) == []
        later = store.add(
            Perception(
                "qq",
                "visitor",
                "后来",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", "busy", "later"),
            )
        )
        assert later is not None
        assert store.list_unread(key) == [later]
    finally:
        timeline.close()


@pytest.mark.asyncio
async def test_active_conversation_merges_during_planning_and_handles_priority(
    tmp_path: Path,
) -> None:
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
                    return plan_response(request, {"reply": False, "stay_in_conversation": False})
                return plan_response(
                    request, {"reply": True, "intent": "回应", "stay_in_conversation": True}
                )
            assert not request.stream
            replies.append(request)
            return OrganResponse("replyer", "收到。")

    identity = Identity.model_validate(
        {
            "facts": {"name": "测试助手", "birthday": "2026-11-23"},
            "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
            "persona": {"immutable": "温和。"},
        }
    )
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=StreamingOrgans(),
        identity=identity,
        config=HeartbeatConfig(split_reply_enabled=False),
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
        # steady：安静开批 4 秒、活跃开批 7 秒；多拨一点，保证已过截止时间（D142）。
        clock.advance(timedelta(seconds=7))
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
        selected = ConversationKey("qq", "bot", "group", "ongoing")
        other = ConversationKey("qq", "bot", "group", "priority-group")
        assert not heart.is_replying_to(conversation=selected)
        clock.advance(timedelta(seconds=3))
        receive("second-window")
        # 同批换人续发：steady 等 2 秒，从 3 秒前的第二条算起还剩 2 秒。
        assert not plans and heart.seconds_until_next_beat() == 2
        await advance_to_planner()
        assert heart.is_replying_to(conversation=selected)
        assert not heart.is_replying_to(conversation=other)
        assert not heart.is_replying_to(web_visibility=VisibilityScope.USER_ONLY)
        assert all(text in plans[0].prompt for text in ("first-alert", "second-window"))
        await finish_plan()
        assert not heart.is_replying_to(conversation=selected)

        receive("ongoing-message")
        await advance_to_planner()
        receive("next-batch-first")
        receive("next-batch-second")
        # D116：活跃会话规划期间的新消息取消当前规划并合入同一批，旧回复不投递。
        await heart.wait_for_thought()
        assert cancellations == [2]
        assert "next-batch-first" not in plans[-1].prompt
        assert store.list_unread(selected) == []
        await finish_plan()
        await advance_to_planner()
        assert all(
            text in plans[-1].prompt
            for text in ("ongoing-message", "next-batch-first", "next-batch-second")
        )
        await finish_plan()

        receive("priority-elsewhere", sender="owner", group="priority-group")
        await advance_to_planner()
        assert not heart.is_replying_to(conversation=selected)
        assert heart.is_replying_to(conversation=other)
        assert "priority-elsewhere" in plans[-1].prompt
        await finish_plan()
        receive("离开当前群", group="priority-group")
        await advance_to_planner()
        await finish_plan()
        assert len(replies) == 3
        assert all(item.route is not None for item in sent if item.kind is MessageKind.REPLY)
        receive("ordinary-after-leaving", group="priority-group")
        assert not heart.is_thinking
        assert store.list_unread_conversations()[0].unread_count == 1
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_active_conversation_merges_messages_arriving_during_debounce(
    tmp_path: Path,
) -> None:
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
    received: list[int] = []

    class StreamingOrgans:
        async def call(self, request: OrganRequest) -> OrganResponse:
            task = request.context.get("task")
            if task == "inbox_select":
                return OrganResponse(request.organ_id, '{"conversation":1,"count":1}')
            if task == "plan":
                assert request.stream
                plans.append(request)
                started.set()
                await release.wait()
                return plan_response(
                    request, {"reply": True, "intent": "回应", "stay_in_conversation": True}
                )
            assert task == "reply" and not request.stream
            replies.append(request)
            return OrganResponse("replyer", "收到。")

    identity = Identity.model_validate(
        {
            "facts": {"name": "测试助手", "birthday": "2026-11-23"},
            "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
            "persona": {"immutable": "温和。"},
        }
    )
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=StreamingOrgans(),
        identity=identity,
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent: list[OutgoingMessage] = []
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
                route=MessageRoute("bot", "group", "ongoing", text),
            )
        )
        assert result is not None
        received.append(result.id)
        heart.on_inbox_message(result)
        heart.beat()

    async def advance_to_planner() -> None:
        # steady：安静开批 4 秒、活跃开批 7 秒；多拨一点，保证已过截止时间（D142）。
        clock.advance(timedelta(seconds=7))
        heart.beat()
        await asyncio.wait_for(started.wait(), timeout=1)
        started.clear()

    async def finish_plan() -> None:
        release.set()
        await heart.wait_for_thought()
        heart.beat()
        release.clear()

    try:
        receive("暖场", sender="owner")
        await advance_to_planner()
        await finish_plan()
        assert len(replies) == 1 and len(sent) == 1

        receive("第一条")
        assert len(plans) == 1
        # 上一条暖场在 7 秒前，新批次按活跃开批等 7 秒（D142）。
        assert heart.seconds_until_next_beat() == 7
        clock.advance(timedelta(seconds=2))
        receive("第二条")
        assert len(plans) == 1
        assert heart.seconds_until_next_beat() == 4
        clock.advance(timedelta(seconds=2))
        heart.beat()
        assert len(plans) == 1
        clock.advance(timedelta(seconds=2))
        heart.beat()
        await asyncio.wait_for(started.wait(), timeout=1)
        started.clear()

        assert len(plans) == 2
        assert all(text in plans[-1].prompt for text in ("第一条", "第二条"))
        assert len(plans[-1].context["in_reply_to"]) == 2
        await finish_plan()
        assert len(sent) == 2
        assert all(text in dialogue_text(replies[-1]) for text in ("第一条", "第二条"))
        assert len(replies[-1].context["in_reply_to"]) == 2

        inbound = [
            record.metadata["inbox_message_id"]
            for record in timeline.query(TimelineQuery(event_type="message", limit=100))
            if "inbox_message_id" in record.metadata
        ]
        assert inbound == received
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_active_conversation_interrupts_planning_and_merges_batch(
    tmp_path: Path,
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    plans: list[OrganRequest] = []
    replies: list[OrganRequest] = []
    received: list[int] = []

    class InterruptibleOrgans:
        async def call(self, request: OrganRequest) -> OrganResponse:
            task = request.context.get("task")
            if task == "inbox_select":
                return OrganResponse(request.organ_id, '{"conversation":1,"count":1}')
            if task == "plan":
                assert request.stream
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
            assert task == "reply" and not request.stream
            replies.append(request)
            return OrganResponse("replyer", "收到。")

    identity = Identity.model_validate(
        {
            "facts": {"name": "测试助手", "birthday": "2026-11-23"},
            "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
            "persona": {"immutable": "温和。"},
        }
    )
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=InterruptibleOrgans(),
        identity=identity,
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent: list[OutgoingMessage] = []
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
                route=MessageRoute("bot", "group", "ongoing", text),
            )
        )
        assert result is not None
        received.append(result.id)
        heart.on_inbox_message(result)
        heart.beat()

    async def advance_to_planner() -> None:
        # steady：安静开批 4 秒、活跃开批 7 秒；多拨一点，保证已过截止时间（D142）。
        clock.advance(timedelta(seconds=7))
        heart.beat()
        await asyncio.wait_for(started.wait(), timeout=1)
        started.clear()

    async def finish_plan() -> None:
        release.set()
        await heart.wait_for_thought()
        heart.beat()
        release.clear()

    try:
        receive("暖场", sender="owner")
        await advance_to_planner()
        await finish_plan()
        assert len(sent) == 1

        receive("m1")
        await advance_to_planner()
        for number in range(2, 6):
            receive(f"m{number}")
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            cancelled.clear()
            await heart.wait_for_thought()
            heart.beat()
            assert len(sent) == 1
            await advance_to_planner()
            assert len(plans[-1].context["in_reply_to"]) == number
        assert all(f"m{number}" in plans[-1].prompt for number in range(1, 6))

        receive("m6")
        await asyncio.sleep(0.05)
        assert not cancelled.is_set()
        assert heart.is_thinking
        assert len(sent) == 1

        release.set()
        await heart.wait_for_thought()
        heart.beat()
        release.clear()
        assert len(sent) == 2
        assert len(replies[-1].context["in_reply_to"]) == 5

        await advance_to_planner()
        assert "m6" in plans[-1].prompt
        assert len(plans[-1].context["in_reply_to"]) == 1
        await finish_plan()
        assert len(sent) == 3

        assert [len(request.context["in_reply_to"]) for request in plans] == [
            1,
            1,
            2,
            3,
            4,
            5,
            1,
        ]
        assert [len(request.context["in_reply_to"]) for request in replies] == [1, 5, 1]
        assert len(timeline.query(TimelineQuery(event_type=EVENT_PLANNING_INTERRUPTED))) == 4
        inbound = [
            record.metadata["inbox_message_id"]
            for record in timeline.query(TimelineQuery(event_type="message", limit=100))
            if "inbox_message_id" in record.metadata
        ]
        assert inbound == received
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["selection", "plan", "delivery"])
async def test_priority_preemption_discards_stale_selection_reply_or_segments(
    tmp_path: Path, phase: str
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def finish_late() -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()

    class PreemptedOrgans:
        old_ids: list[int] | None = None

        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.context["task"] == "inbox_select":
                if phase == "selection":
                    await finish_late()
                return OrganResponse(request.organ_id, '{"conversation":1,"count":1}')
            if request.context["task"] == "plan":
                if "原会话消息" in request.prompt:
                    self.old_ids = request.context["in_reply_to"]
                    if phase == "plan":
                        await finish_late()
                    return plan_response(
                        request, {"reply": True, "intent": "过时回复", "quote_source": True}
                    )
                return plan_response(request, {"reply": True, "intent": "优先回复"})
            text = (
                "原会话先发出这一段。原会话不能再发这一段。"
                if request.context["in_reply_to"] == self.old_ids
                else "优先会话已收到。"
            )
            return OrganResponse(request.organ_id, text)

    identity = Identity.model_validate(
        {
            "facts": {"name": "测试助手", "birthday": "2026-11-23"},
            "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
            "persona": {"immutable": "温和。"},
        }
    )
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=PreemptedOrgans(),
        identity=identity,
        config=HeartbeatConfig(
            message_debounce_mode="off",
            planner_message_steering=False,
            typing_delay_enabled=True,
            typing_jitter_min_seconds=10,
            typing_jitter_max_seconds=10,
        ),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent: list[OutgoingMessage] = []
    heart.attach_channel("qq", sent.append, can_quote=lambda _route: True)
    heart.wake_up()

    def receive(text: str, sender: str, group: str) -> None:
        row = store.add(
            Perception(
                "qq",
                sender,
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", group, text),
            )
        )
        assert row is not None
        heart.on_inbox_message(row)

    try:
        receive("原会话消息", "visitor", "old")
        heart.beat()
        clock.advance(timedelta(minutes=5))
        heart.beat()
        if phase != "selection":
            await heart.wait_for_thought()
            heart.beat()
        if phase == "delivery":
            await heart.wait_for_thought()
            heart.beat()
            assert [message.content for message in sent] == ["原会话先发出这一段。"]
            assert sent[0].quote_source is True
            assert timeline.query(TimelineQuery(event_type=EVENT_REPLY_DELIVERED)) == []
        else:
            await asyncio.wait_for(entered.wait(), timeout=1)
        old_scope = ConversationKey("qq", "bot", "group", "old")
        assert heart.is_replying_to(conversation=old_scope) is (phase == "plan")
        receive("优先会话消息", "owner", "urgent")
        assert not heart.is_replying_to(conversation=old_scope)
        if phase == "delivery":
            assert sent[0].can_deliver is not None and not sent[0].can_deliver()
        else:
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            await heart.wait_for_thought()
        clock.advance(timedelta(seconds=30))
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert len(sent) == (2 if phase == "delivery" else 1)
        assert sent[-1].route is not None and sent[-1].route.recipient_id == "urgent"
        assert sent[-1].confirm_delivery is not None
        sent[-1].confirm_delivery()
        if phase == "delivery":
            assert sent[0].confirm_delivery is not None
            sent[0].confirm_delivery()
        references = timeline.query(TimelineQuery(event_type=EVENT_REPLY_REFERENCE))
        interruptions = timeline.query(TimelineQuery(event_type=EVENT_PLANNING_INTERRUPTED))
        receipts = timeline.query(TimelineQuery(event_type=EVENT_REPLY_DELIVERED))
        failures = timeline.query(TimelineQuery(event_type=EVENT_REPLY_DELIVERY_FAILED))
        assert receipts[0].metadata["round_id"] == references[-1].metadata["round_id"]
        if phase == "delivery":
            [failure] = failures
            assert failure.metadata["segment"] == 2
            assert receipts[-1].metadata["segment"] == 1
            assert failure.metadata["round_id"] == receipts[-1].metadata["round_id"]
            assert receipts[-1].metadata["round_id"] != receipts[0].metadata["round_id"]
            assert interruptions == []
        elif phase == "plan":
            [interruption] = interruptions
            assert "抢占" in interruption.content
            assert interruption.metadata["round_id"] != receipts[0].metadata["round_id"]
            assert failures == []
        else:
            assert interruptions == [] and failures == []
        unread = store.list_unread_conversations()
        assert [row.key.recipient_id for row in unread] == (["old"] if phase == "selection" else [])
    finally:
        await heart.shut_down()
        timeline.close()


def test_mentions_wake_but_do_not_preempt(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    people = PeopleDirectory(database, clock)
    people.open()
    try:
        people.ensure_user("主人", [("qq", "owner")])
        friend = people.meet("qq", "friend")
        attention = InboxAttention(
            InboxStore(database, clock), timeline, people, [], enabled=True, read_limit=35
        )
        timeline.append(
            "special_contact_changed",
            "添加特别关心。",
            ActorType.ME,
            metadata={"person_id": friend.id, "add": True},
        )
        attention.restore()
        mention = Perception(
            "qq",
            "visitor",
            "测试助手在吗",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            mentioned=True,
        )
        owner = Perception(
            "qq", "owner", "在吗", VisibilityScope.USER_ONLY, TrustLevel.VERIFIED_USER
        )
        special = Perception(
            "qq", "friend", "在吗", VisibilityScope.PRIVATE, TrustLevel.VERIFIED_CONTACT
        )
        unverified = Perception(
            "qq",
            "stranger",
            "测试助手在吗",
            VisibilityScope.GROUP,
            TrustLevel.UNVERIFIED,
            mentioned=True,
        )
        assert attention.is_priority(mention)
        assert not attention.preempts(mention)
        assert attention.is_priority(owner) and attention.preempts(owner)
        assert attention.is_priority(special) and attention.preempts(special)
        assert not attention.is_priority(unverified)
        assert not attention.preempts(unverified)
    finally:
        people.close()
        timeline.close()


def test_priority_messages_include_mentions_and_dedupe(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    store = InboxStore(database, clock)
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    try:
        attention = InboxAttention(
            store, timeline, None, [("qq", "owner")], enabled=True, read_limit=35
        )

        def save(sender: str, content: str, *, mentioned: bool = False) -> InboxMessage:
            row = store.add(
                Perception(
                    "qq",
                    sender,
                    content,
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", "group", "group", content),
                    mentioned=mentioned,
                )
            )
            assert row is not None
            return row

        owner_mentioned = save("owner", "点名主人", mentioned=True)
        mention = save("visitor", "点名助手", mentioned=True)
        ordinary = save("visitor", "普通消息")
        assert [row.id for row in attention.priority_messages()] == [
            owner_mentioned.id,
            mention.id,
        ]
        assert ordinary.id not in {row.id for row in attention.priority_messages()}
    finally:
        timeline.close()


@pytest.mark.asyncio
async def test_group_mention_wakes_and_is_answered_without_autonomous_browsing(
    tmp_path: Path,
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    people = PeopleDirectory(database, clock)
    people.open()
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
                    "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                    "user": {"name": "主人"},
                    "persona": {"immutable": "温和。"},
                }
            ),
            config=HeartbeatConfig(
                split_reply_enabled=False,
                message_debounce_mode="off",
                planner_message_steering=False,
            ),
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            people=people,
            inbox_store=store,
        )
        sent: list[OutgoingMessage] = []
        heart.attach_channel("qq", sent.append)
        heart.wake_up()
        perception = Perception(
            "qq",
            "visitor",
            "测试助手在吗",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", "group", "mention"),
            mentioned=True,
        )
        message = store.add(perception)
        assert message is not None
        heart.on_inbox_message(message)
        heart.beat()
        assert heart.is_thinking
        await heart.wait_for_thought()
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert sent[-1].route == perception.route
        assert store.list_unread_conversations() == []
        assert {request.context.get("task") for request in organs.requests} >= {"plan", "reply"}
        await heart.shut_down()
    finally:
        people.close()
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
                    "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                    "user": {"name": "主人"},
                    "persona": {"immutable": "温和。"},
                }
            ),
            config=HeartbeatConfig(
                split_reply_enabled=False,
                message_debounce_mode="off",
                planner_message_steering=False,
            ),
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
            contact_private: bool = False,
            attachments: tuple[MediaReference, ...] = (),
        ) -> Perception:
            kind = "private" if (private or contact_private) else "group"
            route = MessageRoute("bot", kind, sender if kind == "private" else "group", message_id)
            perception = Perception(
                "qq",
                sender,
                f"消息 {message_id}",
                VisibilityScope.USER_ONLY
                if private
                else (VisibilityScope.PRIVATE if contact_private else VisibilityScope.GROUP),
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
        organs.plan = (
            '{"reply":true,"intent":"回应","special_contact":true,"stay_in_conversation":false}'
        )
        contact = receive(
            "visitor", "private-contact", contact_private=True, attachments=(picture,)
        )
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
        assert sent[-1].route == contact.route
        assert reads == [(contact, picture)]
        assert "识别出的内容" in organs.requests[-2].prompt
        assert "private-only" not in organs.requests[-2].prompt
        assert store.add(contact) is None
        changes = timeline.query(TimelineQuery(event_type="special_contact_changed"))
        assert len(changes) == 1
        assert changes[0].metadata["add"] is True
        organs.plan = '{"reply":true,"intent":"回应"}'

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
        # D145：优先来信带出同会话最新一批，先前那条普通消息一并读入并清空，不再留成积压。
        assert sum(row.unread_count for row in store.list_conversations()) == 0
        assert sum(item.route == priority.route for item in sent) == 1
        assert ordinary.route is not None
        assert not any(item.route == ordinary.route for item in sent)

        heart.set_maintenance_paused(True)
        buffered = receive("owner", "maintenance-buffered", private=True)
        heart.beat()
        assert heart.maintenance_paused and not heart.is_thinking
        assert not any(item.route == buffered.route for item in sent)
        assert sum(row.unread_count for row in store.list_conversations()) == 1

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


@pytest.mark.asyncio
async def test_idle_residence_makes_room_for_a_mention_in_another_group(tmp_path: Path) -> None:
    """D137/D138：驻留会话空闲时，别群的提及不必等它自己离开。"""
    clock = ManualClock(datetime(2026, 11, 23, 10, tzinfo=UTC))
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
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent: list[OutgoingMessage] = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()

    def receive(
        text: str, *, sender: str = "visitor", group: str = "one", mentioned: bool = False
    ) -> None:
        message = store.add(
            Perception(
                "qq",
                sender,
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER if sender == "owner" else TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", group, text),
                mentioned=mentioned,
            )
        )
        assert message is not None
        heart.on_inbox_message(message)

    try:
        receive("暖场", sender="owner", group="one")
        heart.beat()
        clock.advance(timedelta(seconds=4))
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        inbox = heart._platform_inbox
        assert inbox is not None
        assert sent[-1].route is not None and sent[-1].route.recipient_id == "one"
        assert inbox.active_conversation == ConversationKey("qq", "bot", "group", "one")

        receive("在吗", group="two", mentioned=True)
        heart.beat()
        clock.advance(timedelta(seconds=4))
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert sent[-1].route is not None and sent[-1].route.recipient_id == "two"
        assert inbox.active_conversation == ConversationKey("qq", "bot", "group", "two")
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_pending_segments_survive_a_mention_from_another_group(tmp_path: Path) -> None:
    """D137：旧驻留还有分段在打字时，提及不能把没发完的回复截断。"""
    clock = ManualClock(datetime(2026, 11, 23, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)

    class SegmentedOrgans(Organs):
        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.context.get("task") == "reply":
                self.requests.append(request)
                return OrganResponse(request.organ_id, "第一段<n>第二段")
            return await super().call(request)

    organs = SegmentedOrgans()
    organs.plan = '{"reply":true,"intent":"回应","stay_in_conversation":true}'
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(
            message_debounce_mode="off",
            planner_message_steering=False,
            split_reply_enabled=True,
            typing_delay_enabled=True,
            typing_jitter_min_seconds=10,
            typing_jitter_max_seconds=10,
        ),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent: list[OutgoingMessage] = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()

    def receive(
        text: str, *, sender: str = "visitor", group: str = "one", mentioned: bool = False
    ) -> None:
        message = store.add(
            Perception(
                "qq",
                sender,
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER if sender == "owner" else TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", group, text),
                mentioned=mentioned,
            )
        )
        assert message is not None
        heart.on_inbox_message(message)

    try:
        receive("暖场", sender="owner", group="one")
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert [item.content for item in sent] == ["第一段"]
        inbox = heart._platform_inbox
        assert inbox is not None
        assert inbox.active_conversation == ConversationKey("qq", "bot", "group", "one")

        receive("在吗", group="two", mentioned=True)
        heart.beat()
        await asyncio.sleep(0)
        assert [item.content for item in sent] == ["第一段"]
        assert inbox.active_conversation == ConversationKey("qq", "bot", "group", "one")

        clock.advance(timedelta(seconds=30))
        heart.beat()
        assert [item.content for item in sent] == ["第一段", "第二段"]
        await heart.wait_for_thought()
        heart.beat()
        assert sent[-1].route is not None and sent[-1].route.recipient_id == "two"
    finally:
        await heart.shut_down()
        timeline.close()


def test_priority_messages_put_identity_before_old_mentions(tmp_path: Path) -> None:
    """D137：一条用户来信不能被 20 条旧提及挤出优先名单。"""
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    store = InboxStore(database, clock)
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    try:
        attention = InboxAttention(
            store, timeline, None, [("qq", "owner")], enabled=True, read_limit=35
        )

        def save(number: int, sender: str, *, mentioned: bool = False) -> InboxMessage:
            row = store.add(
                Perception(
                    "qq",
                    sender,
                    str(number),
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", "group", "group", str(number)),
                    mentioned=mentioned,
                )
            )
            assert row is not None
            return row

        mentions = [save(number, "visitor", mentioned=True) for number in range(20)]
        owner = save(99, "owner")
        result = attention.priority_messages()
        assert result[0].id == owner.id
        assert len(result) == 20
        assert {row.id for row in result[1:]} == {row.id for row in mentions[:19]}
    finally:
        timeline.close()


def test_new_attention_recovers_unread_mentions_but_skips_disallowed_routes(
    tmp_path: Path,
) -> None:
    """D137：重启后新实例补收未读提及；已移出范围的会话和已处理的提及不再返回。"""
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    store = InboxStore(database, clock)
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    try:

        def save(group: str, content: str) -> InboxMessage:
            row = store.add(
                Perception(
                    "qq",
                    "visitor",
                    content,
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", "group", group, content),
                    mentioned=True,
                )
            )
            assert row is not None
            return row

        allowed = save("allowed", "在吗")
        save("former", "旧群在吗")
        attention = InboxAttention(store, timeline, None, [], enabled=True, read_limit=35)
        attention.allows = lambda key: key.recipient_id == "allowed"
        assert [row.id for row in attention.priority_messages()] == [allowed.id]

        timeline.append(
            "message",
            "已处理",
            ActorType.PERSON,
            metadata={"inbox_message_id": allowed.id},
        )
        assert attention.priority_messages() == []
    finally:
        timeline.close()


def test_group_member_cannot_become_a_special_contact(tmp_path: Path) -> None:
    """D137：特别关心只在普通私聊里生效，群聊里的建议一律忽略。"""
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    people = PeopleDirectory(database, clock)
    people.open()
    try:
        person = people.meet("qq", "visitor")
        attention = InboxAttention(
            InboxStore(database, clock), timeline, people, [], enabled=True, read_limit=35
        )
        source = timeline.append("message", "在吗", ActorType.PERSON)
        plan = Plan(True, "回应", None, "", special_contact=True)
        group = Perception(
            "qq",
            "visitor",
            "在吗",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", "group", "one"),
        )
        private = Perception(
            "qq",
            "visitor",
            "在吗",
            VisibilityScope.PRIVATE,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "private", "visitor", "two"),
        )
        attention.apply_plan(plan, group, source)
        assert timeline.query(TimelineQuery(event_type="special_contact_changed")) == []
        assert person.id not in attention._special

        attention.apply_plan(plan, private, source)
        [change] = timeline.query(TimelineQuery(event_type="special_contact_changed"))
        assert change.metadata["person_id"] == person.id
        assert change.metadata["add"] is True
    finally:
        people.close()
        timeline.close()


def test_take_in_perceptions_queues_mentions_before_contacts(tmp_path: Path) -> None:
    """D137：提及排在普通消息之前，但仍排在用户和特别关心之后。"""
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
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
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    try:
        ordinary = Perception(
            "qq",
            "visitor",
            "普通消息",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", "one", "ordinary"),
        )
        mention = Perception(
            "qq",
            "visitor",
            "被点名",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", "two", "mention"),
            mentioned=True,
        )
        ordinary_row = store.add(ordinary)
        mention_row = store.add(mention)
        assert ordinary_row is not None and mention_row is not None
        heart._pending.extend([(ordinary, ordinary_row), (mention, mention_row)])
        heart._take_in_perceptions()
        [first] = heart._unanswered.peek_batch()
        assert first.perception.content == "被点名"
    finally:
        timeline.close()


def test_on_inbox_message_survives_person_lookup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """认人查库失败只记日志，不能把 OneBot 读取任务带崩。"""
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    people = PeopleDirectory(database, clock)
    people.open()
    store = InboxStore(database, clock)
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
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        people=people,
        inbox_store=store,
    )
    heart.attach_channel("qq", lambda _message: None)
    heart.wake_up()
    try:
        message = store.add(
            Perception(
                "qq",
                "visitor",
                "在吗",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", "group", "one"),
            )
        )
        assert message is not None

        def broken(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("模拟读库失败")

        monkeypatch.setattr(people, "find", broken)
        heart.on_inbox_message(message)
        assert not heart.is_thinking
    finally:
        timeline.close()
        people.close()


def test_on_inbox_message_does_not_preempt_for_a_mention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D137：提及只叫醒；只有绑定用户和特别关心的来信才调用抢占。"""
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=Organs(),
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    heart.attach_channel("qq", lambda _message: None)
    heart.wake_up()
    preempted: list[ConversationKey] = []
    monkeypatch.setattr(heart, "_preempt_inbox", lambda _inbox, key: preempted.append(key))
    try:

        def save(sender: str, group: str, *, mentioned: bool = False) -> InboxMessage:
            row = store.add(
                Perception(
                    "qq",
                    sender,
                    "在吗",
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("bot", "group", group, sender),
                    mentioned=mentioned,
                )
            )
            assert row is not None
            return row

        heart.on_inbox_message(save("visitor", "one", mentioned=True))
        assert preempted == []
        heart.on_inbox_message(save("owner", "two"))
        assert [key.recipient_id for key in preempted] == ["two"]
    finally:
        timeline.close()
