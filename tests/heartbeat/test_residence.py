"""D148：用假时钟、临时收件箱核对驻留及续期。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import Perception, TrustLevel
from aureus.heartbeat.inbox_attention import InboxAttention, InboxSelection
from aureus.heartbeat.inbox_store import InboxMessage, InboxStore
from aureus.heartbeat.perception import MessageRoute
from aureus.heartbeat.planning import Plan
from aureus.timeline import ActorType, TimelineDatabase, TimelineRecord, VisibilityScope

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


def save(store: InboxStore, group: str, message: str) -> InboxMessage:
    row = store.add(
        Perception(
            "qq",
            "visitor",
            message,
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", group, message),
        )
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
