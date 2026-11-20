"""主动联系的发送顺序与故障恢复回归：只用假器官和临时数据库（D76）。"""

import sqlite3
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from aureus.common import BodyConfig, InitiativeConfig
from aureus.feedback import PROACTIVE_CONTACT, Confidence, Outcome
from aureus.heartbeat import (
    EVENT_INITIATIVE_CONSIDERED,
    EVENT_INITIATIVE_RESULT,
    EVENT_MESSAGE_DELIVERY,
    OutgoingMessage,
)
from aureus.heartbeat.initiative_history import read_initiative_history
from aureus.timeline import ActorType, TimelineRecord
from tests.heartbeat.test_initiative_flow import Env, consider, finish, submit
from tests.heartbeat.test_judging_flow import CALENDAR, CHANNEL, NOW

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    opened = Env(tmp_path)
    yield opened
    for rig in opened.rigs:
        await rig.heartbeat.shut_down()
    for store in reversed(opened.closers):
        store.close()


async def test_queued_user_message_before_send_is_not_an_answer(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    env.clock.advance(timedelta(hours=20))
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    assert not rig.records(EVENT_INITIATIVE_CONSIDERED)
    submit(rig)  # 用户来信先进入队列，此时主动消息还没有发出。
    rig.heartbeat.beat()
    assert rig.records(EVENT_INITIATIVE_CONSIDERED)[-1].metadata["sent"] is True
    assert not rig.records(EVENT_INITIATIVE_RESULT)
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 0
    await finish(rig)
    assert not rig.records(EVENT_INITIATIVE_RESULT)

    # 时钟不动也能区分前后；只有发送后收到的新来信才能结算这次联系。
    await rig.exchange("我在")
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is True
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1


@pytest.mark.parametrize("failure", ["feedback", "timeline"])
@pytest.mark.parametrize("answer_hours", [1, 6, 7])
async def test_answer_settlement_survives_restart(
    env: Env, monkeypatch: pytest.MonkeyPatch, failure: str, answer_hours: int
) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    rig = env.build(policy=policy)
    await rig.exchange("你好")
    await consider(env, rig)
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    original_append = rig.timeline.append

    def broken_append(**kwargs: Any) -> TimelineRecord:
        if kwargs.get("event_type") == EVENT_INITIATIVE_RESULT:
            raise sqlite3.OperationalError("测试结果暂时写不了")
        return original_append(**kwargs)

    def broken_outcome(record_id: int, outcome: Outcome) -> None:
        raise sqlite3.OperationalError("测试反馈暂时写不了")

    with monkeypatch.context() as patch:
        if failure == "feedback":
            patch.setattr(env.feedback, "record_outcome", broken_outcome)
        else:
            patch.setattr(rig.timeline, "append", broken_append)
        env.clock.advance(timedelta(hours=answer_hours))
        await rig.exchange("我在")
        env.clock.advance(timedelta(hours=7))
        await rig.exchange("又来一句")  # 重启时不能只看最后这条较晚的来信。
        assert not rig.records(EVENT_INITIATIVE_RESULT)
        for _ in range(60):
            rig.timeline.append(event_type="message", content="普通聊天", actor_type=ActorType.ME)
        await rig.heartbeat.shut_down()

    restored = env.build(policy=policy)
    restored.heartbeat.beat()
    expected = Outcome.SUCCESS if answer_hours < policy.answer_window_hours else Outcome.FAILURE
    feedback = env.feedback.get(considered.metadata["feedback_id"])
    assert feedback is not None and feedback.outcome is expected
    [result] = restored.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is (expected is Outcome.SUCCESS)
    assert result.metadata["message_id"] == considered.metadata["message_id"]
    assert restored.heartbeat._initiative_state.unanswered_streak == 0
    for _ in range(2):
        restored.heartbeat.beat()
    await restored.heartbeat.shut_down()
    restarted = env.build(policy=policy)
    restarted.heartbeat.beat()
    assert len(restarted.records(EVENT_INITIATIVE_RESULT)) == 1
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1


async def test_missing_considered_record_does_not_bypass_daily_limit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    body = BodyConfig(sleep_enabled=False, social_need_time_constant_hours=0.1)
    rig = env.build(policy=policy, body=body)
    await rig.exchange("你好")
    env.clock.advance(timedelta(hours=1))
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    original_append = rig.timeline.append

    def broken_append(**kwargs: Any) -> TimelineRecord:
        if kwargs.get("event_type") == EVENT_INITIATIVE_CONSIDERED:
            raise sqlite3.OperationalError("测试考虑记录暂时写不了")
        return original_append(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rig.timeline, "append", broken_append)
        rig.heartbeat.beat()
        assert rig.heartbeat._initiative_state.contacts_today == 1
        assert not rig.records(EVENT_INITIATIVE_CONSIDERED)
        await rig.heartbeat.shut_down()

    restored = env.build(policy=policy, body=body)
    assert restored.heartbeat._initiative_state.contacts_today == 1
    await restored.exchange("我在")
    [recovered] = restored.records(EVENT_INITIATIVE_CONSIDERED)
    assert recovered.metadata["sent"] is True
    assert "送达凭据" in recovered.metadata["failure"]
    env.clock.advance(timedelta(hours=4))
    restored.heartbeat.beat()
    await finish(restored)
    sent = [row for row in restored.records("message") if row.metadata.get("initiative")]
    assert len(sent) == policy.max_per_day
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("answered", [False, True])
async def test_failed_feedback_expectation_is_eventually_recorded(
    env: Env, monkeypatch: pytest.MonkeyPatch, restart: bool, committed: bool, answered: bool
) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    rig = env.build(policy=policy)
    await rig.exchange("你好")
    env.clock.advance(timedelta(hours=20))
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    original_expectation = env.feedback.record_expectation

    def broken_expectation(*args: Any, **kwargs: Any) -> int:
        if committed:
            original_expectation(*args, **kwargs)
        raise sqlite3.OperationalError("测试预期写入结果不明")

    def confidence(category: str) -> Confidence:
        return Confidence(category, 0.8, 10, insufficient=False)

    with monkeypatch.context() as patch:
        patch.setattr(env.feedback, "record_expectation", broken_expectation)
        patch.setattr(env.feedback, "confidence", confidence)
        rig.heartbeat.beat()
        if restart:
            await rig.heartbeat.shut_down()
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert considered.metadata["sent"] is True
    if restart:
        rig = env.build(policy=policy)
    env.clock.advance(timedelta(hours=1 if answered else 6))
    if answered:
        await rig.exchange("我在")
    else:
        rig.heartbeat.beat()
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is answered
    [feedback] = env.feedback.outcomes_since(NOW)
    assert feedback.outcome is (Outcome.SUCCESS if answered else Outcome.FAILURE)
    assert feedback.source_id == considered.metadata["message_id"]
    assert feedback.expected == 0.8 and feedback.expected_at == considered.timestamp
    for _ in range(2):
        rig.heartbeat.beat()
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1
    assert len(env.feedback.outcomes_since(NOW)) == 1


@pytest.mark.parametrize("event_type", [EVENT_INITIATIVE_CONSIDERED, EVENT_INITIATIVE_RESULT])
async def test_timeline_commit_then_error_is_not_duplicated(
    env: Env, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    rig = env.build(policy=InitiativeConfig(enabled=True, max_per_day=1))
    await rig.exchange("你好")
    original_append = rig.timeline.append

    def broken_append(**kwargs: Any) -> TimelineRecord:
        record = original_append(**kwargs)
        if kwargs.get("event_type") == event_type:
            raise sqlite3.OperationalError("测试追加已成功但调用方收到错误")
        return record

    with monkeypatch.context() as patch:
        patch.setattr(rig.timeline, "append", broken_append)
        await consider(env, rig)
        env.clock.advance(timedelta(hours=1))
        await rig.exchange("我在")
    rig.heartbeat.beat()
    assert len(rig.records(EVENT_INITIATIVE_CONSIDERED)) == 1
    assert len(rig.records(EVENT_INITIATIVE_RESULT)) == 1
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1


async def test_failed_delivery_is_not_counted_after_restart(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")

    def broken(message: OutgoingMessage) -> None:
        raise OSError("测试发送失败")

    rig.heartbeat._sinks[CHANNEL] = broken
    await consider(env, rig)
    await rig.heartbeat.shut_down()
    restored = env.build()
    state = restored.heartbeat._initiative_state
    assert state.contacts_today == 0 and state.pending_since is None
    assert not env.feedback.outcomes_since(NOW)


async def test_full_restore_window_blocks_when_daily_count_is_uncertain(env: Env) -> None:
    rig = env.build()
    for _ in range(50):
        rig.timeline.append("message", "主动消息", ActorType.ME, metadata={"initiative": True})
    policy = InitiativeConfig(enabled=True, max_per_day=100)
    history = read_initiative_history(rig.timeline, CALENDAR, env.clock.now(), policy)
    assert history.state.contacts_today == policy.max_per_day


async def test_failed_delivery_outside_considered_window_has_no_feedback(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")

    def broken(message: OutgoingMessage) -> None:
        raise OSError("测试发送失败")

    rig.heartbeat._sinks[CHANNEL] = broken
    await consider(env, rig)
    [failed] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert failed.metadata["sent"] is False
    for _ in range(51):
        env.clock.advance(timedelta(hours=3))
        rig.timeline.append(
            event_type=EVENT_INITIATIVE_CONSIDERED,
            content="没有想说的",
            actor_type=ActorType.ME,
            metadata={"sent": False, "decided": "skip"},
        )
    await rig.heartbeat.shut_down()
    restored = env.build()
    restored.heartbeat.beat()
    assert restored.heartbeat._initiative_state.pending_since is None
    assert restored.heartbeat._initiative_state.contacts_today == 0
    assert len(restored.records(EVENT_INITIATIVE_CONSIDERED)) == 52
    assert not restored.records(EVENT_INITIATIVE_RESULT)
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 0
    assert env.feedback.find_by_source(PROACTIVE_CONTACT, failed.metadata["message_id"]) is None


async def test_message_commit_then_error_does_not_invent_contact_feedback(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = env.build(policy=InitiativeConfig(enabled=True, max_per_day=1))
    await rig.exchange("你好")
    rig.sent.clear()
    original_append = rig.timeline.append

    def broken_append(**kwargs: Any) -> TimelineRecord:
        record = original_append(**kwargs)
        if kwargs.get("event_type") == "message" and kwargs.get("metadata", {}).get("initiative"):
            raise sqlite3.OperationalError("测试消息已落盘但调用报错，尚未发送")
        return record

    with monkeypatch.context() as patch:
        patch.setattr(rig.timeline, "append", broken_append)
        await consider(env, rig)
    assert not rig.sent
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 0
    await rig.heartbeat.shut_down()
    env.clock.advance(timedelta(hours=6))
    restored = env.build()
    restored.heartbeat.beat()
    [result] = restored.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["delivery_confirmed"] is False
    unknown = restored.records(EVENT_INITIATIVE_CONSIDERED)[-1]
    assert unknown.metadata["decided"] == "unknown" and unknown.metadata["sent"] is None
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 0
    assert not env.feedback.outcomes_since(NOW)


@pytest.mark.parametrize("missing_expectation", [False, True])
@pytest.mark.parametrize("answered", [False, True])
async def test_missing_delivery_evidence_never_creates_feedback(
    env: Env, monkeypatch: pytest.MonkeyPatch, missing_expectation: bool, answered: bool
) -> None:
    rig = env.build(policy=InitiativeConfig(enabled=True, max_per_day=1))
    await rig.exchange("你好")
    original_append = rig.timeline.append

    def broken_append(**kwargs: Any) -> TimelineRecord:
        if kwargs.get("event_type") in (EVENT_MESSAGE_DELIVERY, EVENT_INITIATIVE_CONSIDERED):
            raise sqlite3.OperationalError("测试收据和考虑记录均未留存")
        return original_append(**kwargs)

    def broken_expectation(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("测试预期也未留存")

    with monkeypatch.context() as patch:
        patch.setattr(rig.timeline, "append", broken_append)
        if missing_expectation:
            patch.setattr(env.feedback, "record_expectation", broken_expectation)
        await consider(env, rig)
        await rig.heartbeat.shut_down()
    restored = env.build(policy=InitiativeConfig(enabled=True, max_per_day=1))
    env.clock.advance(timedelta(hours=1 if answered else 6))
    if answered:
        await restored.exchange("我在")
    else:
        restored.heartbeat.beat()
    [result] = restored.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is answered
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == int(not missing_expectation)
    if missing_expectation:
        assert result.metadata["delivery_confirmed"] is False
        assert restored.records(EVENT_INITIATIVE_CONSIDERED)[0].metadata["decided"] == "unknown"


async def test_answer_window_restores_the_actual_delivery_time(env: Env) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    rig = env.build(policy=policy)
    await rig.exchange("你好")

    def delivered(message: OutgoingMessage) -> None:
        env.clock.advance(timedelta(seconds=30))
        rig.sent.append(message)

    rig.heartbeat._sinks[CHANNEL] = delivered
    await consider(env, rig)
    actual = env.clock.now()
    assert rig.heartbeat._initiative_state.pending_since == actual
    await rig.heartbeat.shut_down()
    restored = env.build(policy=policy)
    assert restored.heartbeat._initiative_state.pending_since == actual
    env.clock.advance(timedelta(hours=6) - timedelta(seconds=15))
    await restored.exchange("我在")
    [result] = restored.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is True
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1
