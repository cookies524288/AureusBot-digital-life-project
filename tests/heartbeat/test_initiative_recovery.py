"""主动联系只恢复消息事实；写入失败不补记，来信保留先后顺序（D76、D78）。"""

import sqlite3
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from aureus.common import BodyConfig, InitiativeConfig
from aureus.feedback import PROACTIVE_CONTACT, Outcome
from aureus.heartbeat import EVENT_INITIATIVE_CONSIDERED, EVENT_INITIATIVE_RESULT, OutgoingMessage
from aureus.heartbeat.initiative_history import read_initiative_history
from aureus.timeline import ActorType, TimelineRecord
from tests.heartbeat.test_initiative_flow import Env, consider, finish, submit
from tests.heartbeat.test_judging_flow import CALENDAR, CHANNEL

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
    submit(rig)
    rig.heartbeat.beat()
    assert not rig.records(EVENT_INITIATIVE_RESULT)
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 0
    await finish(rig)
    # 同一个时钟刻度上，发送后收到的新消息才是回应。
    await rig.exchange("我在")
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is True
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1


async def test_missing_considered_record_does_not_bypass_daily_limit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    rig = env.build(
        policy=policy, body=BodyConfig(sleep_enabled=False, social_need_time_constant_hours=0.1)
    )
    await rig.exchange("你好")
    original = rig.timeline.append

    def broken(*args: Any, **kwargs: Any) -> TimelineRecord:
        # 投递确认等调用方用位置参数传事件类型，两种写法都要认。
        event_type = args[0] if args else kwargs.get("event_type")
        if event_type == EVENT_INITIATIVE_CONSIDERED:
            raise sqlite3.OperationalError("测试考虑记录写不进去")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rig.timeline, "append", broken)
        await consider(env, rig)
    [message] = [record for record in rig.records("message") if record.metadata.get("initiative")]
    assert message.metadata["reasons"] and message.metadata["social_need"] > 0.7
    assert message.metadata["due_item_ids"] == [] and message.metadata["plan"]["reach_out"] is True
    assert message.metadata["initiative_expected"] == 0.5
    await rig.heartbeat.shut_down()
    restored = env.build(policy=policy)
    assert restored.heartbeat._initiative.state.contacts_today == 1
    await restored.exchange("我在")
    env.clock.advance(timedelta(hours=4))
    restored.heartbeat.beat()
    assert not restored.heartbeat.is_thinking
    assert not restored.records(EVENT_INITIATIVE_CONSIDERED)  # 不补考虑记录。
    assert len(restored.records(EVENT_INITIATIVE_RESULT)) == 1


@pytest.mark.parametrize("answer_hours", [1, 6, 7])
async def test_answer_settlement_survives_restart(
    env: Env, monkeypatch: pytest.MonkeyPatch, answer_hours: int
) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    rig = env.build(policy=policy)
    await rig.exchange("你好")
    await consider(env, rig)
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    original = rig.timeline.append

    def broken(*args: Any, **kwargs: Any) -> TimelineRecord:
        event_type = args[0] if args else kwargs.get("event_type")
        if event_type == EVENT_INITIATIVE_RESULT:
            raise sqlite3.OperationalError("测试回应结果写不进去")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rig.timeline, "append", broken)
        env.clock.advance(timedelta(hours=answer_hours))
        await rig.exchange("我在")
    assert rig.heartbeat._initiative.state.pending_since is None
    env.clock.advance(timedelta(hours=7))
    rig.heartbeat.beat()
    assert not rig.records(EVENT_INITIATIVE_RESULT)  # 本次运行没有待补材料。
    await rig.heartbeat.shut_down()
    restored = env.build(policy=policy)
    restored.heartbeat.beat()
    [result] = restored.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata == {
        "message_id": considered.metadata["message_id"],
        "answered": answer_hours < 6,
    }
    feedback = env.feedback.find_by_source(PROACTIVE_CONTACT, considered.metadata["message_id"])
    assert feedback is not None and feedback.outcome is (
        Outcome.SUCCESS if answer_hours < 6 else Outcome.FAILURE
    )
    assert restored.heartbeat._initiative.state.unanswered_streak == 0
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1


async def test_response_result_is_written_once(env: Env) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    rig = env.build(policy=policy)
    await rig.exchange("你好")
    await consider(env, rig)
    env.clock.advance(timedelta(hours=1))
    await rig.exchange("我在")
    for _ in range(3):
        rig.heartbeat.beat()
    await rig.heartbeat.shut_down()
    restored = env.build(policy=policy)
    restored.heartbeat.beat()
    await restored.exchange("还有事吗")
    assert len(restored.records(EVENT_INITIATIVE_RESULT)) == 1
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1


async def test_message_record_failure_prevents_delivery(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = env.build()
    await rig.exchange("你好")
    rig.sent.clear()
    original = rig.timeline.append

    def broken(*args: Any, **kwargs: Any) -> TimelineRecord:
        event_type = args[0] if args else kwargs.get("event_type")
        if event_type == "message":
            raise sqlite3.OperationalError("测试消息写不进去")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rig.timeline, "append", broken)
        await consider(env, rig)
    [record] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert record.metadata["decided"] == "failed" and record.metadata["sent"] is False
    assert not rig.sent and rig.heartbeat._initiative.state.contacts_today == 0
    assert rig.heartbeat._initiative.state.pending_since is None


@pytest.mark.parametrize("answered", [False, True])
async def test_missing_feedback_expectation_does_not_prevent_result(
    env: Env, monkeypatch: pytest.MonkeyPatch, answered: bool
) -> None:
    policy = InitiativeConfig(enabled=True, max_per_day=1)
    rig = env.build(policy=policy)
    await rig.exchange("你好")

    def broken(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("测试反馈预期写不进去")

    with monkeypatch.context() as patch:
        patch.setattr(env.feedback, "record_expectation", broken)
        await consider(env, rig)
    [considered] = rig.records(EVENT_INITIATIVE_CONSIDERED)
    assert considered.metadata["sent"] is True and considered.metadata["feedback_id"] is None
    env.clock.advance(timedelta(hours=1 if answered else 6))
    if answered:
        await rig.exchange("我在")
    else:
        rig.heartbeat.beat()
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is answered
    assert env.feedback.find_by_source(PROACTIVE_CONTACT, considered.metadata["message_id"]) is None
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 0
    await rig.heartbeat.shut_down()
    restored = env.build(policy=policy)
    restored.heartbeat.beat()
    assert len(restored.records(EVENT_INITIATIVE_RESULT)) == 1
    assert env.feedback.find_by_source(PROACTIVE_CONTACT, considered.metadata["message_id"]) is None


async def test_failed_delivery_still_uses_daily_limit(env: Env) -> None:
    rig = env.build(policy=InitiativeConfig(enabled=True, max_per_day=1))
    await rig.exchange("你好")

    def broken(message: OutgoingMessage) -> None:
        raise OSError("测试通道发送失败")

    rig.heartbeat._sinks[CHANNEL] = broken
    await consider(env, rig)
    await rig.heartbeat.shut_down()
    restored = env.build()
    assert restored.heartbeat._initiative.state.contacts_today == 1
    assert restored.heartbeat._initiative.state.pending_since is not None
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 0


async def test_full_restore_window_blocks_when_daily_count_is_uncertain(env: Env) -> None:
    rig = env.build()
    for _ in range(50):
        rig.timeline.append("message", "主动开口", ActorType.ME, metadata={"initiative": True})
    policy = InitiativeConfig(enabled=True, max_per_day=100)
    history = read_initiative_history(rig.timeline, CALENDAR, env.clock.now(), policy)
    assert history.state.contacts_today == policy.max_per_day
