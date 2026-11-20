"""主动联系的完整流程：真实原因、可选调用、退避、反馈和重启恢复（D76）。"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from aureus.agenda import AgendaStore
from aureus.body import Body, EnergyTier
from aureus.common import (
    AgendaConfig,
    BodyConfig,
    FeedbackConfig,
    HeartbeatConfig,
    InitiativeConfig,
    ManualClock,
    MoodConfig,
)
from aureus.feedback import PROACTIVE_CONTACT, FeedbackStore, Outcome
from aureus.heartbeat import (
    EVENT_INITIATIVE_CONSIDERED,
    EVENT_INITIATIVE_RESULT,
    Heartbeat,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.identity import Identity
from aureus.mood import Mood
from aureus.organs import CallAttempt, CallPurpose, OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import CallRejectedError, EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase, VisibilityScope
from tests.heartbeat.test_agenda_flow import Store
from tests.heartbeat.test_judging_flow import (
    CALENDAR,
    CHANNEL,
    MONTHLY_LIMIT,
    NOW,
    PLAN,
    Rig,
    ScriptedOrgans,
)

pytestmark = pytest.mark.asyncio
REACH = '{"reach_out":true,"intent":"问问近况","reason":"看到了流星"}'
SKIP = '{"reach_out":false,"reason":"没有想说的"}'
DECLINE = '{"reply":false,"reason":"不用回"}'


class InitiativeOrgans(ScriptedOrgans):
    """按用途分别给出预设建议；可阻塞主动调用来测消息竞争和取消。"""

    def __init__(self) -> None:
        super().__init__(None)
        self.plans: list[str | BaseException] = []
        self.replies: list[str | BaseException] = []
        self.normal_plans: list[str] = []
        self.block: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        task = request.context.get("task")
        if task in ("initiative_plan", "initiative_reply"):
            self.started.set()
            if self.block is not None:
                await self.block.wait()
            queue = self.plans if task == "initiative_plan" else self.replies
            result = (
                queue.pop(0) if queue else REACH if task == "initiative_plan" else "最近怎么样？"
            )
            if isinstance(result, BaseException):
                raise result
        elif request.organ_id == "planner":
            result = self.normal_plans.pop(0) if self.normal_plans else PLAN
        else:
            result = "好的。"
        return OrganResponse(organ_id=request.organ_id, suggestion=result)


class Env:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = ManualClock(NOW)
        self.closers: list[Store] = []
        self.rigs: list[Rig] = []

    def build(
        self,
        *,
        policy: InitiativeConfig | None = None,
        body: BodyConfig | None = None,
        mood: Mood | None = None,
        heartbeat: HeartbeatConfig | None = None,
        monthly_limit: int | None = MONTHLY_LIMIT,
    ) -> Rig:
        path = self.path / "aureus.db"
        ledger = Ledger(path, self.clock, calendar=CALENDAR)
        timeline = TimelineDatabase(path, self.clock)
        self.people = PeopleDirectory(path, self.clock)
        self.feedback = FeedbackStore(path, FeedbackConfig(), self.clock)
        self.agenda = AgendaStore(path, self.clock, AgendaConfig())
        for opened in (ledger, timeline, self.people, self.feedback, self.agenda):
            opened.open()
            self.closers.append(opened)
        self.people.ensure_user("阿明", [(CHANNEL, "local"), ("web", "web-user")])
        stop = EmergencyStop(self.path / "halt.json", self.clock)
        self.organs = InitiativeOrgans()
        beating = Heartbeat(
            clock=self.clock,
            timeline=timeline,
            organs=self.organs,
            identity=Identity.model_validate(
                {
                    "facts": {"name": "小绿", "birthday": "2008-01-01"},
                    "user": {"name": "阿明"},
                    "persona": {"immutable": "温和。"},
                }
            ),
            config=heartbeat
            or HeartbeatConfig(shutdown_grace_seconds=0, split_reply_enabled=False),
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            timezone=UTC,
            body=Body(
                body or BodyConfig(sleep_enabled=False), monthly_limit, ledger, self.clock, CALENDAR
            ),
            people=self.people,
            feedback=self.feedback,
            agenda=self.agenda,
            mood=mood,
            initiative=policy or InitiativeConfig(enabled=True),
            calendar=CALENDAR,
        )
        rig = Rig(ledger, timeline, self.organs, beating)
        beating.attach_channel(CHANNEL, rig.sent.append)
        beating.wake_up()
        self.rigs.append(rig)
        return rig


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    opened = Env(tmp_path)
    yield opened
    for rig in opened.rigs:
        await rig.heartbeat.shut_down()
    for store in reversed(opened.closers):
        store.close()


async def finish(rig: Rig) -> None:
    await rig.heartbeat.wait_for_thought()
    rig.heartbeat.beat()


async def consider(env: Env, rig: Rig, hours: float = 20) -> None:
    env.clock.advance(timedelta(hours=hours))
    rig.heartbeat.beat()
    assert rig.heartbeat.is_thinking
    await finish(rig)


def submit(
    rig: Rig, *, channel: str = CHANNEL, trust: TrustLevel = TrustLevel.VERIFIED_USER
) -> None:
    rig.heartbeat.submit(
        Perception(
            channel=channel,
            sender_id="web-user"
            if channel == "web"
            else "local"
            if trust is TrustLevel.VERIFIED_USER
            else "friend",
            content="在吗",
            visibility=VisibilityScope.USER_ONLY,
            trust=trust,
        )
    )


async def test_disabled_initiative_makes_no_calls_after_a_day(env: Env) -> None:
    rig = env.build(policy=InitiativeConfig())
    await rig.exchange("你好")
    env.organs.requests.clear()
    env.clock.advance(timedelta(hours=24))
    rig.heartbeat.beat()
    assert not env.organs.requests and not rig.heartbeat.is_thinking
    assert not rig.records(EVENT_INITIATIVE_CONSIDERED)


async def test_initiative_has_honest_reasons_optional_calls_and_expectation(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    env.organs.requests.clear()
    rig.sent.clear()
    await consider(env, rig)
    assert [request.organ_id for request in env.organs.requests] == ["planner", "replyer"]
    assert all(request.purpose is CallPurpose.OPTIONAL for request in env.organs.requests)
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    metadata = considered.metadata
    assert metadata["sent"] is True and metadata["decided"] == "reach_out"
    assert metadata["reasons"] == ["距上次和用户好好聊过 20.0 小时，社交需求 81%"]
    assert metadata["social_need"] > 0.8 and metadata["due_item_ids"] == []
    assert "看到了流星" not in "；".join(metadata["reasons"])
    assert metadata["plan"]["reason"] == "看到了流星"
    assert considered.visibility is VisibilityScope.PRIVATE
    [message] = [record for record in rig.records("message") if record.metadata.get("initiative")]
    assert message.metadata["to_user"] is True and message.visibility is VisibilityScope.USER_ONLY
    [sent] = rig.sent
    assert sent.content == message.content and sent.kind is MessageKind.REPLY
    expectation = env.feedback.get(metadata["feedback_id"])
    assert expectation is not None and expectation.category == PROACTIVE_CONTACT
    assert expectation.expected == 0.5 and expectation.source_id == message.id
    context = env.organs.requests[-1].system_context
    assert "这是你主动找阿明" in context and metadata["reasons"][0] in context
    assert "不是在回复" in context and "【身体状态】" in context
    assert rig.heartbeat._last_conversation_at == NOW


async def test_skip_counts_as_consideration_and_waits_three_hours(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    env.organs.plans = [SKIP, SKIP]
    await consider(env, rig)
    [record] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert record.metadata["decided"] == "skip" and record.metadata["sent"] is False
    assert rig.heartbeat._initiative_state.contacts_today == 0
    assert len(rig.replies()) == 1
    env.clock.advance(timedelta(hours=3) - timedelta(seconds=1))
    rig.heartbeat.beat()
    assert not rig.heartbeat.is_thinking
    await consider(env, rig, 1 / 3600)
    assert len(rig.records(EVENT_INITIATIVE_CONSIDERED)) == 2


@pytest.mark.parametrize(
    "failure",
    ["bad_plan", "plan_error", "plan_refused", "reply_error", "reply_refused", "empty_reply"],
)
async def test_organ_failures_are_silent_and_recorded(env: Env, failure: str) -> None:
    rig = env.build()
    await rig.exchange("你好")
    rig.sent.clear()
    if failure == "bad_plan":
        env.organs.plans = ["乱码"]
    elif failure == "plan_error":
        env.organs.plans = [OSError("失败")]
    elif failure == "plan_refused":
        env.organs.plans = [CallRejectedError("planner", "可选额度不足")]
    elif failure == "reply_error":
        env.organs.replies = [OSError("失败")]
    elif failure == "reply_refused":
        env.organs.replies = [CallRejectedError("replyer", "可选额度不足")]
    else:
        env.organs.replies = ["   "]
    await consider(env, rig)
    [record] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert record.metadata["decided"] == "failed" and record.metadata["sent"] is False
    assert record.metadata["failure"] and not rig.sent
    assert rig.heartbeat._last_conversation_at == NOW


async def test_backoff_doubles_then_gives_up_until_user_speaks(env: Env) -> None:
    rig = env.build(
        policy=InitiativeConfig(enabled=True, max_per_day=10),
        body=BodyConfig(
            sleep_enabled=False,
            social_need_time_constant_hours=0.1,
        ),
    )
    await rig.exchange("你好")
    await consider(env, rig)
    env.clock.advance(timedelta(hours=6) - timedelta(seconds=1))
    rig.heartbeat.beat()
    assert not rig.heartbeat.is_thinking
    await consider(env, rig, 1 / 3600)
    considered = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert considered[1].timestamp - considered[0].timestamp == timedelta(hours=6)
    first = env.feedback.get(considered[0].metadata["feedback_id"])
    assert first is not None and first.outcome is Outcome.FAILURE
    env.clock.advance(timedelta(hours=6))
    rig.heartbeat.beat()
    assert rig.heartbeat._initiative_state.unanswered_streak == 2
    assert not rig.heartbeat.is_thinking
    await consider(env, rig, 6)
    considered = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert considered[2].timestamp - considered[1].timestamp == timedelta(hours=12)
    env.clock.advance(timedelta(hours=6))
    rig.heartbeat.beat()
    assert rig.heartbeat._initiative_state.unanswered_streak == 3
    env.clock.advance(timedelta(days=100))
    rig.heartbeat.beat()
    assert not rig.heartbeat.is_thinking
    assert len(rig.records(EVENT_INITIATIVE_CONSIDERED)) == 3
    assert [record.metadata["answered"] for record in rig.records(EVENT_INITIATIVE_RESULT)] == [
        False
    ] * 3
    await rig.exchange("我回来了")
    assert rig.heartbeat._initiative_state.unanswered_streak == 0
    await consider(env, rig, 3)
    assert len(rig.records(EVENT_INITIATIVE_CONSIDERED)) == 4


async def test_user_answer_records_success_and_resets_waiting(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    await consider(env, rig)
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    env.clock.advance(timedelta(hours=1))
    await rig.exchange("最近还好")
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata == {
        "answered": True,
        "considered_id": considered.id,
        "message_id": considered.metadata["message_id"],
    }
    feedback = env.feedback.get(considered.metadata["feedback_id"])
    assert feedback is not None and feedback.outcome is Outcome.SUCCESS
    state = rig.heartbeat._initiative_state
    assert state.unanswered_streak == 0 and state.pending_since is None
    assert rig.heartbeat._last_conversation_at == env.clock.now()


async def test_answer_at_the_window_boundary_is_failure_but_resets_backoff(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    await consider(env, rig)
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    env.clock.advance(timedelta(hours=6))
    env.organs.normal_plans = [DECLINE]
    await rig.exchange("回来了")
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata == {
        "answered": False,
        "considered_id": considered.id,
        "message_id": considered.metadata["message_id"],
    }
    feedback = env.feedback.get(considered.metadata["feedback_id"])
    assert feedback is not None and feedback.outcome is Outcome.FAILURE
    assert rig.heartbeat._initiative_state.unanswered_streak == 0
    assert rig.heartbeat._initiative_state.pending_since is None
    assert rig.heartbeat._last_conversation_at == NOW


async def test_contact_messages_do_not_settle_the_users_pending_contact(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    await consider(env, rig)
    pending = rig.heartbeat._initiative_state.pending_since
    env.clock.advance(timedelta(hours=1))
    submit(rig, trust=TrustLevel.VERIFIED_CONTACT)
    rig.heartbeat.beat()
    await finish(rig)
    assert rig.heartbeat._initiative_state.pending_since == pending
    assert not rig.records(EVENT_INITIATIVE_RESULT)
    assert rig.heartbeat._last_conversation_at == NOW


@pytest.mark.parametrize("gate", ["night", "energy", "stress", "halt"])
async def test_night_energy_stress_and_halt_block_initiative(env: Env, gate: str) -> None:
    body = BodyConfig(sleep_enabled=gate == "night", stress_window_calls=2, stress_high_at=0.5)
    rig = env.build(body=body)
    await rig.exchange("你好")
    env.organs.requests.clear()
    env.clock.advance(timedelta(hours=37 if gate == "night" else 20))
    if gate == "energy":
        rig.ledger.record(
            "replyer", [CallAttempt("m", "p", prompt_tokens=200_000, completion_tokens=0)]
        )
    elif gate == "stress":
        rig.ledger.record(
            "replyer",
            [
                CallAttempt(
                    "m",
                    "p",
                    prompt_tokens=1,
                    completion_tokens=0,
                    succeeded=False,
                    failure_reason="失败",
                )
            ],
        )
    elif gate == "halt":
        rig.heartbeat.halt("测试")
    rig.heartbeat.beat()
    assert not rig.heartbeat.is_thinking and not env.organs.requests
    assert not rig.records(EVENT_INITIATIVE_CONSIDERED)
    if gate == "stress":
        assert rig.heartbeat._current_strategy().effective_tier is EnergyTier.SAVING


async def test_cautious_mood_allows_only_one_contact_per_day(env: Env) -> None:
    mood = Mood(MoodConfig(half_life_seconds=86400.0))
    rig = env.build(
        mood=mood, body=BodyConfig(sleep_enabled=False, social_need_time_constant_hours=0.1)
    )
    await rig.exchange("你好")
    for _ in range(5):
        env.feedback.record_outcome(
            env.feedback.record_expectation("sim_task:hard", 0.5), Outcome.FAILURE
        )
    await consider(env, rig, 1)
    assert mood.cautious
    env.clock.advance(timedelta(minutes=1))
    await rig.exchange("在")
    env.clock.advance(timedelta(hours=3))
    rig.heartbeat.beat()
    assert mood.cautious and not rig.heartbeat.is_thinking
    assert len(rig.records(EVENT_INITIATIVE_CONSIDERED)) == 1


async def test_due_commitment_alone_can_prompt_initiative(env: Env) -> None:
    rig = env.build(body=BodyConfig(sleep_enabled=False, social_need_enabled=False))
    await rig.exchange("你好")
    person = env.people.find(CHANNEL, "local")
    assert person is not None
    item = env.agenda.add(
        person.id,
        "提醒他开会",
        due_at=NOW + timedelta(hours=1),
        source_record_id=None,
        feedback_id=None,
    )
    assert item is not None
    await consider(env, rig, 1)
    [record] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert record.metadata["reasons"] == [f"答应的事到期了：#{item.id} 提醒他开会"]
    assert record.metadata["due_item_ids"] == [item.id]
    context = env.organs.requests[-1].system_context
    assert "【日程】" in context and f"#{item.id} 提醒他开会" in context
    assert env.agenda.open_items(person.id)  # 主动提醒不擅自结束约定。


async def test_user_messages_take_priority_over_initiative(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    env.organs.requests.clear()
    env.clock.advance(timedelta(hours=20))
    submit(rig)
    rig.heartbeat.beat()
    await finish(rig)
    assert all(request.purpose is CallPurpose.USER_REPLY for request in env.organs.requests)
    assert not rig.records(EVENT_INITIATIVE_CONSIDERED)


async def test_a_user_arriving_during_initiative_waits_for_the_same_slot(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    env.organs.block = asyncio.Event()
    env.clock.advance(timedelta(hours=20))
    rig.heartbeat.beat()
    await env.organs.started.wait()
    submit(rig)
    rig.heartbeat.beat()
    assert len(rig.heartbeat._unanswered) == 1
    env.organs.block.set()
    await finish(rig)
    assert rig.heartbeat.is_thinking  # 主动结束后才开始回复刚到的用户消息。
    await finish(rig)
    assert len(rig.records(EVENT_INITIATIVE_CONSIDERED)) == 1


async def test_restart_restores_pending_count_and_settles_timeout_without_channel(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    await consider(env, rig)
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    await rig.heartbeat.shut_down()
    env.clock.advance(timedelta(hours=7))
    restored = env.build()
    state = restored.heartbeat._initiative_state
    assert state.contacts_today == 1 and state.pending_since == considered.timestamp
    assert restored.heartbeat._last_conversation_at == NOW
    assert restored.heartbeat._last_user_contact is None
    restored.heartbeat.beat()
    state = restored.heartbeat._initiative_state
    assert state.unanswered_streak == 1 and state.pending_since is None
    assert not restored.heartbeat.is_thinking
    [result] = restored.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata == {
        "answered": False,
        "considered_id": considered.id,
        "message_id": considered.metadata["message_id"],
    }
    feedback = env.feedback.get(considered.metadata["feedback_id"])
    assert feedback is not None and feedback.outcome is Outcome.FAILURE


async def test_restart_keeps_the_daily_limit_even_after_user_returns(env: Env) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    body = BodyConfig(sleep_enabled=False, social_need_time_constant_hours=0.1)
    rig = env.build(policy=policy, body=body)
    await rig.exchange("你好")
    await consider(env, rig, 1)
    await rig.heartbeat.shut_down()
    restored = env.build(policy=policy, body=body)
    assert restored.heartbeat._initiative_state.contacts_today == 1
    await restored.exchange("我在")
    env.clock.advance(timedelta(hours=4))
    restored.heartbeat.beat()
    assert not restored.heartbeat.is_thinking
    assert len(restored.records(EVENT_INITIATIVE_CONSIDERED)) == 1


async def test_restart_restores_consecutive_failures_and_latest_consideration(env: Env) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=10)
    rig = env.build(policy=policy)
    await rig.exchange("你好")
    await consider(env, rig)
    await consider(env, rig, 6)
    env.clock.advance(timedelta(hours=6))
    rig.heartbeat.beat()
    await rig.heartbeat.shut_down()
    restored = env.build(policy=policy)
    state = restored.heartbeat._initiative_state
    assert state.unanswered_streak == 2 and state.pending_since is None
    assert state.last_considered_at == restored.records(EVENT_INITIATIVE_CONSIDERED)[-1].timestamp
    await restored.exchange("回来了")
    assert restored.heartbeat._initiative_state.unanswered_streak == 0


async def test_latest_user_channel_is_used(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    rig.heartbeat.attach_channel("web", rig.sent.append)
    submit(rig, channel="web")
    rig.heartbeat.beat()
    await finish(rig)
    rig.sent.clear()
    await consider(env, rig)
    [message] = rig.sent
    assert message.channel == "web" and message.recipient_id == "web-user"


@pytest.mark.parametrize("failure", ["disconnected", "sink", "halt"])
async def test_send_failures_do_not_count_as_contacts(env: Env, failure: str) -> None:
    rig = env.build()
    await rig.exchange("你好")
    rig.sent.clear()
    env.clock.advance(timedelta(hours=20))
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    if failure == "disconnected":
        rig.heartbeat._sinks.clear()
    elif failure == "sink":

        def broken(message: OutgoingMessage) -> None:
            raise OSError("通道失败")

        rig.heartbeat._sinks[CHANNEL] = broken
    else:
        rig.heartbeat.halt("不许发")
    rig.heartbeat.beat()
    [record] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert record.metadata["sent"] is False and record.metadata["decided"] == "failed"
    if failure == "disconnected":
        assert record.metadata["failure"] == "通道不在了"
    assert rig.heartbeat._initiative_state.contacts_today == 0
    assert rig.heartbeat._initiative_state.pending_since is None
    assert not rig.sent and rig.heartbeat._last_conversation_at == NOW


async def test_shutdown_cancels_initiative_without_a_notice(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    rig.sent.clear()
    env.organs.block = asyncio.Event()
    env.clock.advance(timedelta(hours=20))
    rig.heartbeat.beat()
    await env.organs.started.wait()
    await rig.heartbeat.shut_down()
    [record] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert record.metadata["decided"] == "failed"
    assert record.metadata["failure"] == "关闭时取消"
    assert not record.metadata["sent"] and not rig.sent


async def test_missing_planning_disables_initiative_with_one_warning(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        rig = env.build(heartbeat=HeartbeatConfig(planning_enabled=False))
    await rig.exchange("你好")
    env.clock.advance(timedelta(hours=24))
    rig.heartbeat.beat()
    rig.heartbeat.beat()
    assert not rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert sum("主动联系要用规划器" in record.message for record in caplog.records) == 1


async def test_agenda_read_failure_does_not_stop_the_heartbeat(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = env.build()
    await rig.exchange("你好")

    def broken(person_id: int | None = None) -> list:
        raise sqlite3.OperationalError("测试故障")

    monkeypatch.setattr(env.agenda, "open_items", broken)
    env.clock.advance(timedelta(hours=20))
    rig.heartbeat.beat()
    assert not rig.heartbeat.is_thinking and not rig.records(EVENT_INITIATIVE_CONSIDERED)


@pytest.mark.parametrize("source", ["body", "feedback", "history"])
async def test_unavailable_state_blocks_initiative_for_this_beat(
    env: Env, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    rig = env.build()
    await rig.exchange("你好")
    env.organs.requests.clear()

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("测试故障")

    with monkeypatch.context() as patch:
        if source == "body":
            patch.setattr(rig.heartbeat._body, "read", broken)
        elif source == "feedback":
            patch.setattr(env.feedback, "confidence", broken)
        else:
            patch.setattr(rig.timeline, "query", broken)
        env.clock.advance(timedelta(hours=20))
        rig.heartbeat.beat()
        assert not rig.heartbeat.is_thinking and not env.organs.requests
    assert not rig.records(EVENT_INITIATIVE_CONSIDERED)
    rig.heartbeat.beat()
    assert rig.heartbeat.is_thinking
    await finish(rig)
    assert rig.records(EVENT_INITIATIVE_CONSIDERED)[-1].metadata["sent"] is True


async def test_message_record_failure_prevents_delivery(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = env.build()
    await rig.exchange("你好")
    rig.sent.clear()
    env.clock.advance(timedelta(hours=20))
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    original = rig.timeline.append

    def broken(**kwargs):
        if kwargs.get("event_type") == "message":
            raise sqlite3.OperationalError("测试故障")
        return original(**kwargs)

    monkeypatch.setattr(rig.timeline, "append", broken)
    rig.heartbeat.beat()
    [record] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert record.metadata["decided"] == "failed" and not record.metadata["sent"]
    assert not rig.sent and rig.heartbeat._initiative_state.contacts_today == 0
    assert rig.heartbeat._initiative_state.pending_since is None


async def test_timeout_write_failure_is_retried_without_duplicate_feedback(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = env.build(policy=InitiativeConfig(enabled=True, max_per_day=1))
    await rig.exchange("你好")
    await consider(env, rig)
    original = rig.timeline.append

    def broken(**kwargs):
        if kwargs.get("event_type") == EVENT_INITIATIVE_RESULT:
            raise sqlite3.OperationalError("测试故障")
        return original(**kwargs)

    monkeypatch.setattr(rig.timeline, "append", broken)
    env.clock.advance(timedelta(hours=6))
    rig.heartbeat.beat()
    assert rig.heartbeat._initiative_state.pending_since is not None
    assert not rig.heartbeat.is_thinking
    monkeypatch.setattr(rig.timeline, "append", original)
    rig.heartbeat.beat()
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is False
    assert rig.heartbeat._initiative_state.unanswered_streak == 1


@pytest.mark.parametrize("failure", ["feedback", "timeline"])
async def test_answer_write_failure_keeps_the_original_result_for_retry(
    env: Env, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    rig = env.build()
    await rig.exchange("你好")
    await consider(env, rig)
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    original_append = rig.timeline.append
    original_outcome = env.feedback.record_outcome

    def broken_append(**kwargs):
        if kwargs.get("event_type") == EVENT_INITIATIVE_RESULT:
            raise sqlite3.OperationalError("测试故障")
        return original_append(**kwargs)

    def broken_outcome(*args, **kwargs):
        raise sqlite3.OperationalError("测试故障")

    if failure == "feedback":
        monkeypatch.setattr(env.feedback, "record_outcome", broken_outcome)
    else:
        monkeypatch.setattr(rig.timeline, "append", broken_append)
    env.clock.advance(timedelta(hours=1))
    await rig.exchange("我在")
    assert not rig.records(EVENT_INITIATIVE_RESULT)
    assert rig.heartbeat._initiative_state.unanswered_streak == 0
    env.clock.advance(timedelta(hours=6))
    monkeypatch.setattr(rig.timeline, "append", original_append)
    monkeypatch.setattr(env.feedback, "record_outcome", original_outcome)
    rig.heartbeat.beat()
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata == {
        "answered": True,
        "considered_id": considered.id,
        "message_id": considered.metadata["message_id"],
    }
    feedback = env.feedback.get(considered.metadata["feedback_id"])
    assert feedback is not None and feedback.outcome is Outcome.SUCCESS
    assert rig.heartbeat._initiative_state.pending_since is None
    assert rig.heartbeat._initiative_state.unanswered_streak == 0
