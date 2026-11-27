"""D148：用假时钟、假规划器和临时收件箱核对驻留及续期。"""

import asyncio
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
from aureus.safety import (
    ACTION_CALL_ORGAN,
    ACTION_REPLY_TO_USER,
    EmergencyStop,
    PermissionChecker,
    PermissionLevel,
)
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)
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
    quote: str | None = None,
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
            quoted_message_id=quote,
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
    assert inbox.browse(can_browse=False) == []
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
        self.stay: bool | None = True
        self.reply = True
        self.response = "收到。"
        self.wait: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.selections = 0

    async def call(self, request: OrganRequest) -> OrganResponse:
        if request.context.get("task") == "plan":
            self.plans.append(request)
            if self.wait is not None:
                self.entered.set()
                await self.wait.wait()
            plan = {"reply": self.reply, "intent": "回应"}
            if self.stay is not None:
                plan["stay_in_conversation"] = self.stay
            return plan_response(request, plan)
        if request.context.get("task") == "inbox_select":
            self.selections += 1
            return OrganResponse(request.organ_id, '{"conversation":1,"count":1}')
        return OrganResponse(request.organ_id, self.response)


@asynccontextmanager
async def running_heart(
    tmp_path: Path,
    config: HeartbeatConfig,
    organs: Organs,
    *,
    confirms_delivery: bool = False,
    reject_first: bool = False,
    levels: dict[str, PermissionLevel] | None = None,
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
        permissions=PermissionChecker(stop) if levels is None else PermissionChecker(stop, levels),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent: list[OutgoingMessage] = []

    def send(message: OutgoingMessage) -> None:
        sent.append(message)
        if reject_first and len(sent) == 1:
            raise RuntimeError("合成通道同步拒绝")

    heart.attach_channel("qq", send, confirms_delivery=confirms_delivery)
    timeline.open()
    try:
        heart.wake_up()
        yield heart, store, clock, sent
    finally:
        if organs.wait is not None:
            organs.wait.set()
        await heart.shut_down()
        timeline.close()


def active_inbox(heart: Heartbeat) -> InboxAttention:
    inbox = heart._platform_inbox
    assert inbox is not None
    return inbox


async def finish_round(heart: Heartbeat) -> None:
    heart.beat()
    await heart.wait_for_thought()
    heart.beat()


def immediate_config(**fields: object) -> HeartbeatConfig:
    return HeartbeatConfig.model_validate(
        {
            "message_debounce_mode": "off",
            "planner_message_steering": False,
            "typing_delay_enabled": False,
            **fields,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_mention_pull_summary_has_no_content(tmp_path: Path, enabled: bool) -> None:
    organs = Organs()
    config = immediate_config(mention_pull_enabled=enabled)
    async with running_heart(tmp_path, config, organs) as (heart, store, clock, _):
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
        heart.beat()
        records = heart._timeline.query(TimelineQuery(event_type="reply_reference"))
        metadata = records[-1].metadata
        assert "residence_deadline" not in records[0].metadata
        assert metadata["residence_deadline"] == (clock.now() + WINDOW).isoformat()
        if enabled:
            assert metadata["mention_pull"] == {
                "conversations": [
                    {"name": "会话 first", "count": 3},
                    {"name": "会话 second", "count": 2},
                    {"name": "会话 third", "count": 1},
                ],
                "others": 1,
            }
        else:
            assert "mention_pull" not in metadata


@pytest.mark.asyncio
async def test_mention_pull_planner_can_leave_or_stay(tmp_path: Path) -> None:
    organs = Organs()
    async with running_heart(tmp_path, immediate_config(), organs) as (heart, store, _, sent):
        current = save(store, "current", "入场", mentioned=True)
        heart.on_inbox_message(current)
        await finish_round(heart)
        inbox = active_inbox(heart)
        assert inbox.active_conversation == current.conversation

        other = save(store, "other", "别群点名", mentioned=True)
        heart.on_inbox_message(other)
        heart.beat()
        assert heart._thought is None and inbox.active_conversation == current.conversation
        assert store.list_unread(other.conversation) == []
        for text in ("第一轮继续", "第二轮继续"):
            heart.on_inbox_message(save(store, "current", text))
            await finish_round(heart)
            assert '群"会话 other" 1 条' in organs.plans[-1].prompt
            assert inbox.active_conversation == current.conversation
            assert all(
                message.route and message.route.recipient_id == "current" for message in sent
            )

        organs.stay = False
        heart.on_inbox_message(save(store, "current", "这一轮聊完"))
        await finish_round(heart)
        assert '群"会话 other" 1 条' in organs.plans[-1].prompt
        assert inbox.active_conversation is None
        await finish_round(heart)
        assert "别群点名" in organs.plans[-1].prompt and "收件箱动态" not in organs.plans[-1].prompt
        assert sent[-1].route and sent[-1].route.recipient_id == "other"


@pytest.mark.asyncio
async def test_quote_without_mention_keeps_legacy_idle_switch(tmp_path: Path) -> None:
    organs = Organs()
    async with running_heart(tmp_path, immediate_config(), organs) as (heart, store, _, sent):
        heart.on_inbox_message(save(store, "current", "入场", mentioned=True))
        await finish_round(heart)
        store.add_delivered(
            Perception(
                "qq",
                "bot",
                "旧回复",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", "other", "old-reply"),
            ),
            sender_name="测试助手",
        )
        quote = save(store, "other", "只引用旧答复", quote="old-reply")
        assert not quote.perception.mentioned
        heart.on_inbox_message(quote)
        await finish_round(heart)
        assert sent[-1].route and sent[-1].route.recipient_id == "other"
        assert "收件箱动态" not in organs.plans[-1].prompt


@pytest.mark.asyncio
async def test_idle_expiry_does_not_cut_active_thought(tmp_path: Path) -> None:
    organs = Organs()
    config = immediate_config(planner_message_steering=True)
    async with running_heart(tmp_path, config, organs) as (heart, store, clock, _):
        current = save(store, "current", "入场", mentioned=True)
        heart.on_inbox_message(current)
        await finish_round(heart)
        inbox = active_inbox(heart)
        residence = inbox.residence
        assert residence is not None
        organs.reply = False
        organs.wait = asyncio.Event()

        heart.on_inbox_message(save(store, "current", "慢规划"))
        heart.beat()
        await asyncio.wait_for(organs.entered.wait(), timeout=2)
        thought = heart._thought
        assert thought is not None
        deadline = inbox.deadline
        clock.advance(WINDOW)
        heart.on_inbox_message(save(store, "other", "别群点名", mentioned=True))
        heart.beat()
        assert heart._thought is thought and not thought.task.cancelling()
        assert inbox.active_conversation == current.conversation and inbox.deadline == deadline
        assert len(organs.plans) == 2

        organs.stay = None
        organs.wait.set()
        await heart.wait_for_thought()
        heart.beat()
        assert inbox.active_conversation is None and inbox.deadline is None
        await heart.wait_for_thought()
        assert len(organs.plans) == 3 and "别群点名" in organs.plans[-1].prompt


@pytest.mark.asyncio
async def test_window_expires_into_immediate_selection(tmp_path: Path) -> None:
    organs = Organs()
    async with running_heart(tmp_path, immediate_config(), organs) as (heart, store, clock, _):
        current = save(store, "current", "入场", mentioned=True)
        heart.on_inbox_message(current)
        await finish_round(heart)
        inbox = active_inbox(heart)
        waiting = save(store, "other", "等会儿再看")
        clock.advance(WINDOW - timedelta(seconds=1))
        heart.beat()
        assert heart.seconds_until_next_beat() == 1
        assert inbox.active_conversation == current.conversation
        clock.advance(timedelta(seconds=1))
        heart.beat()
        await heart.wait_for_thought()
        assert organs.selections == 1 and inbox.active_conversation is None
        heart.beat()
        assert inbox.active_conversation == waiting.conversation


@pytest.mark.asyncio
async def test_planner_leave_stops_new_batches_and_finishes_segments(tmp_path: Path) -> None:
    organs = Organs()
    config = immediate_config(
        reply_split_mode="explicit",
        typing_delay_enabled=True,
        typing_jitter_min_seconds=10,
        typing_jitter_max_seconds=10,
    )
    async with running_heart(tmp_path, config, organs, confirms_delivery=True) as (
        heart,
        store,
        clock,
        sent,
    ):
        current = save(store, "current", "入场", mentioned=True)
        heart.on_inbox_message(current)
        await finish_round(heart)
        assert sent[0].confirm_delivery is not None
        sent[0].confirm_delivery()

        organs.stay = False
        organs.response = "留下第一段<n>留下第二段"
        organs.wait = asyncio.Event()
        heart.on_inbox_message(save(store, "current", "离开前最后一轮"))
        heart.beat()
        await asyncio.wait_for(organs.entered.wait(), timeout=2)
        ignored = save(store, "current", "不再开批")
        heart.on_inbox_message(ignored)
        heart.on_inbox_message(save(store, "other", "下一会话", mentioned=True))
        heart.beat()
        organs.wait.set()
        await finish_round(heart)
        assert active_inbox(heart).active_conversation is None
        assert len(organs.plans) == 2
        assert [item.content for item in sent] == ["收到。", "留下第一段"]

        clock.advance(timedelta(seconds=30))
        heart.beat()
        assert heart._thought is None
        assert sent[-1].content == "留下第二段"
        for message in sent[1:]:
            assert message.can_deliver is not None and message.can_deliver()
            assert message.confirm_delivery is not None
            message.confirm_delivery()
        await finish_round(heart)
        assert len(organs.plans) == 3 and "下一会话" in organs.plans[-1].prompt
        assert "不再开批" not in organs.plans[-1].prompt
        assert ignored.perception.route is not None
        assert store.find_platform_message(
            ignored.conversation, ignored.perception.route.message_id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["accepted", "rejected", "sync_rejected", "no_receipt"])
async def test_reply_delivery_renews_window_only_on_success(tmp_path: Path, result: str) -> None:
    organs = Organs()
    config = immediate_config()
    async with running_heart(
        tmp_path,
        config,
        organs,
        confirms_delivery=result != "no_receipt",
        reject_first=result == "sync_rejected",
    ) as (heart, store, clock, sent):
        current = save(store, "current", "入场", mentioned=True)
        heart.on_inbox_message(current)
        await finish_round(heart)
        inbox = active_inbox(heart)
        assert inbox.deadline == clock.now() + WINDOW
        clock.advance(WINDOW)
        heart.beat()
        if result in {"sync_rejected", "no_receipt"}:
            assert inbox.active_conversation is None
            return
        assert inbox.active_conversation == current.conversation
        assert inbox.deadline == clock.now()
        if result == "accepted":
            assert sent[0].confirm_delivery is not None
            sent[0].confirm_delivery()
            heart.beat()
            assert inbox.deadline == clock.now() + WINDOW
        else:
            assert sent[0].report_delivery_failure is not None
            sent[0].report_delivery_failure("合成平台拒收")
            heart.beat()
            assert inbox.active_conversation is None


@pytest.mark.asyncio
async def test_verified_user_preempts_and_old_receipt_cannot_renew(tmp_path: Path) -> None:
    organs = Organs()
    async with running_heart(tmp_path, immediate_config(), organs, confirms_delivery=True) as (
        heart,
        store,
        clock,
        sent,
    ):
        current = save(store, "current", "入场", mentioned=True)
        heart.on_inbox_message(current)
        await finish_round(heart)
        inbox = active_inbox(heart)
        old = inbox.residence
        assert old is not None
        clock.advance(timedelta(minutes=1))
        owner = save(store, "owner", "主人来信", sender="owner", trust=TrustLevel.VERIFIED_USER)
        heart.on_inbox_message(owner)
        assert inbox.residence is None and not inbox.permits(old)
        await finish_round(heart)
        assert inbox.active_conversation == owner.conversation
        again = save(store, "current", "主人回原群", sender="owner", trust=TrustLevel.VERIFIED_USER)
        heart.on_inbox_message(again)
        await finish_round(heart)
        assert inbox.active_conversation == current.conversation and inbox.residence is not old
        deadline = inbox.deadline
        clock.advance(timedelta(seconds=5))
        assert sent[0].confirm_delivery is not None
        sent[0].confirm_delivery()
        assert inbox.deadline == deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["route", "permission", "stop"])
async def test_residence_ends_immediately_when_access_is_lost(
    tmp_path: Path, revocation: str
) -> None:
    organs = Organs()
    levels = {
        ACTION_CALL_ORGAN: PermissionLevel.ROUTINE,
        ACTION_REPLY_TO_USER: PermissionLevel.ROUTINE,
    }
    async with running_heart(tmp_path, immediate_config(), organs, levels=levels) as (
        heart,
        store,
        clock,
        _,
    ):
        current = save(store, "current", "入场", mentioned=True)
        heart.on_inbox_message(current)
        await finish_round(heart)
        inbox = active_inbox(heart)
        assert inbox.active_conversation == current.conversation
        if revocation == "route":
            heart.set_inbox_route_filter(lambda _: False)
        elif revocation == "permission":
            levels[ACTION_CALL_ORGAN] = PermissionLevel.FORBIDDEN
        else:
            assert heart._emergency_stop.engage("合成急停")
        heart.beat()
        assert inbox.residence is None and inbox.deadline is None
        assert clock.now() < current.timestamp + WINDOW
