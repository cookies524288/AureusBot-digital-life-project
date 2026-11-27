"""D148：用假时钟、假规划器和临时收件箱核对驻留及续期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import Heartbeat, OutgoingMessage, Perception, TrustLevel
from aureus.heartbeat.inbox_attention import InboxAttention, InboxSelection
from aureus.heartbeat.inbox_store import InboxMessage, InboxStore
from aureus.heartbeat.perception import MessageRoute
from aureus.heartbeat.planning import Plan
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineRecord, VisibilityScope
from tests.planner_fixtures import plan_response

DEFAULT_CONFIG = HeartbeatConfig()
WINDOW = timedelta(seconds=DEFAULT_CONFIG.inbox_residence_seconds)


def make_attention(
    tmp_path: Path, config: HeartbeatConfig = DEFAULT_CONFIG
) -> tuple[ManualClock, InboxAttention]:
    clock = ManualClock(datetime(2026, 11, 27, 12, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    inbox = InboxAttention(
        InboxStore(database, clock),
        TimelineDatabase(database, clock),
        None,
        [],
        enabled=config.inbox_attention_enabled,
        read_limit=config.inbox_read_limit,
        clock=clock,
        residence_seconds=config.inbox_residence_seconds,
        mention_pull_enabled=config.mention_pull_enabled,
    )
    return clock, inbox


def save(
    store: InboxStore,
    group: str,
    message: str,
    *,
    mentioned: bool = False,
    sender: str = "visitor",
    trust: TrustLevel = TrustLevel.VERIFIED_CONTACT,
) -> InboxMessage:
    row = store.add(
        Perception(
            "qq",
            sender,
            message,
            VisibilityScope.GROUP,
            trust,
            route=MessageRoute("bot", "group", group, message),
            mentioned=mentioned,
        ),
        conversation_name=f"会话 {group}",
    )
    assert row is not None
    return row


def test_sliding_window_renews_on_message(tmp_path: Path) -> None:
    clock, inbox = make_attention(tmp_path)
    first = save(inbox.store, "current", "first")
    assert inbox.read_selection(InboxSelection(first.conversation, 1), now=clock.now()) == [first]
    residence = inbox.residence
    assert residence is not None
    assert inbox.deadline == clock.now() + WINDOW

    clock.advance(timedelta(minutes=4))
    save(inbox.store, "other", "unrelated")
    assert inbox.active_messages(now=clock.now()) == []
    assert inbox.deadline == clock.now() + timedelta(minutes=1)
    latest = save(inbox.store, "current", "latest")
    assert inbox.active_messages(now=clock.now()) == [latest]
    assert inbox.deadline == clock.now() + WINDOW
    assert inbox.residence is residence and inbox.permits(residence)

    clock.advance(timedelta(minutes=4))
    assert not inbox.expire_if_due(clock.now())
    clock.advance(timedelta(minutes=1))
    assert inbox.expire_if_due(clock.now())
    assert inbox.active_conversation is None and inbox.deadline is None
    assert inbox.residence is residence and inbox.permits(residence)
    assert not inbox.expire_if_due(clock.now())


def test_expiry_allows_immediate_browse(tmp_path: Path) -> None:
    clock, inbox = make_attention(tmp_path)
    first = save(inbox.store, "current", "first")
    waiting = save(inbox.store, "other", "waiting")
    assert inbox.browse(can_browse=True) == []
    assert inbox.read_selection(InboxSelection(first.conversation, 1), now=clock.now())
    clock.advance(WINDOW)
    assert inbox.browse(can_browse=True) == []
    assert inbox.expire_if_due(clock.now())
    assert [item.key for item in inbox.browse(can_browse=True)] == [waiting.conversation]


def test_planner_stay_renews(tmp_path: Path) -> None:
    clock, inbox = make_attention(tmp_path)
    first = save(inbox.store, "current", "first")
    source = TimelineRecord(
        id=1,
        timestamp=clock.now(),
        actor_type=ActorType.PERSON,
        event_type="message",
        visibility=VisibilityScope.GROUP,
    )
    plan = Plan(True, "回应", None, "", stay_in_conversation=True)
    inbox.apply_plan(plan, first.perception, source, now=clock.now())
    residence = inbox.residence
    assert residence is not None
    assert inbox.active_conversation == first.conversation
    assert inbox.deadline == clock.now() + WINDOW
    clock.advance(timedelta(minutes=4))
    inbox.apply_plan(plan, first.perception, source, now=clock.now())
    assert inbox.residence is residence
    assert inbox.deadline == clock.now() + WINDOW

    inbox.interrupt()
    assert inbox.deadline is None and inbox.active_conversation is None
    assert not inbox.permits(residence)


def test_residence_disabled_by_zero(tmp_path: Path) -> None:
    clock, inbox = make_attention(tmp_path, HeartbeatConfig(inbox_residence_seconds=0))
    first = save(inbox.store, "current", "first")
    assert inbox.read_selection(InboxSelection(first.conversation, 1), now=clock.now())
    clock.advance(timedelta(hours=1))
    inbox.renew(clock.now())
    assert inbox.deadline is None
    assert not inbox.expire_if_due(clock.now())
    assert inbox.active_conversation == first.conversation


def test_mention_counts_exclude_recalled_and_deduplicate_pending_ids(tmp_path: Path) -> None:
    _, inbox = make_attention(tmp_path)
    read = save(inbox.store, "allowed", "已读待答", mentioned=True)
    inbox.store.mark_read([read.id])
    unread = save(inbox.store, "allowed", "还未读", mentioned=True)
    recalled = save(inbox.store, "allowed", "已撤回", mentioned=True)
    assert recalled.perception.route is not None
    inbox.store.mark_recalled(recalled.conversation, recalled.perception.route.message_id)
    save(inbox.store, "allowed", "只是聊天")
    save(inbox.store, "allowed", "未经验证", mentioned=True, trust=TrustLevel.UNVERIFIED)
    save(inbox.store, "denied", "范围外", mentioned=True)
    current = save(inbox.store, "current", "本会话", mentioned=True)
    inbox.allows = lambda key: key.recipient_id != "denied"

    [summary] = inbox.mention_pull_summary(
        current.conversation, pending_ids=[read.id, read.id, unread.id, recalled.id]
    )
    assert (summary.conversation, summary.name, summary.count) == (
        read.conversation,
        "会话 allowed",
        2,
    )


class Organs:
    def __init__(self) -> None:
        self.plans: list[OrganRequest] = []
        self.stay = True

    async def call(self, request: OrganRequest) -> OrganResponse:
        if request.context.get("task") == "plan":
            self.plans.append(request)
            return plan_response(
                request, {"reply": True, "intent": "回应", "stay_in_conversation": self.stay}
            )
        return OrganResponse(request.organ_id, "收到。")


@asynccontextmanager
async def running_heart(
    tmp_path: Path, config: HeartbeatConfig, organs: Organs
) -> AsyncIterator[tuple[Heartbeat, InboxStore, ManualClock, list[OutgoingMessage]]]:
    clock = ManualClock(datetime(2026, 11, 27, 12, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-27"},
                "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=config,
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent: list[OutgoingMessage] = []
    heart.attach_channel("qq", sent.append)
    timeline.open()
    try:
        heart.wake_up()
        yield heart, store, clock, sent
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_mention_pull_summary_has_no_content(tmp_path: Path, enabled: bool) -> None:
    organs = Organs()
    config = HeartbeatConfig(
        mention_pull_enabled=enabled,
        message_debounce_mode="off",
        planner_message_steering=False,
        typing_delay_enabled=False,
    )
    async with running_heart(tmp_path, config, organs) as (heart, store, _, _):
        heart.set_inbox_route_filter(lambda key: key.recipient_id != "excluded")
        heart.on_inbox_message(save(store, "current", "入场", mentioned=True))
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert len(organs.plans) == 1 and "收件箱动态" not in organs.plans[0].prompt

        heart.on_inbox_message(save(store, "current", "继续聊天"))
        for group, count in (("first", 3), ("second", 2), ("third", 1), ("fourth", 1)):
            for offset in range(count):
                heart.on_inbox_message(
                    save(store, group, f"私密原文 {offset}", mentioned=True, sender="sender-secret")
                )
        save(store, "excluded", "范围外原文", mentioned=True)
        heart.beat()
        await heart.wait_for_thought()
        assert len(organs.plans) == 2
        prompt = organs.plans[-1].prompt
        assert "继续聊天" in prompt
        assert all(text not in prompt for text in ("私密原文", "sender-secret", "会话 excluded"))
        if enabled:
            assert '群"会话 first" 3 条' in prompt
            assert '群"会话 second" 2 条' in prompt
            assert '群"会话 third" 1 条' in prompt
            assert "另有 1 个会话" in prompt and "会话 fourth" not in prompt
            assert "当前会话最近 5 分钟有 2 条新消息" in prompt
        else:
            assert "收件箱动态" not in prompt
