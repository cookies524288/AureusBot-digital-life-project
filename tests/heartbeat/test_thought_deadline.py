"""单轮超时只结束该轮生成；用假器官和隔离时间线逐拍验证。"""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from aureus.heartbeat import EVENT_MESSAGE, EVENT_REPLY_REFERENCE, MessageKind
from aureus.heartbeat.events import EVENT_PLANNING_INTERRUPTED, EVENT_REPLY_DELIVERED
from aureus.organs import OrganRequest, OrganResponse
from tests.heartbeat.test_main_loop import Factory, ScriptedOrgans, said


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[Factory]:
    made = Factory(tmp_path / "timeline.db")
    yield made
    made.close()


def shorten_deadline(monkeypatch: pytest.MonkeyPatch, *, from_seconds: float = 180) -> None:
    timeout = asyncio.timeout
    monkeypatch.setattr(
        asyncio, "timeout", lambda delay: timeout(0.02 if delay == from_seconds else delay)
    )


@pytest.mark.asyncio
async def test_deadline_cancels_generation_without_fake_reply(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    shorten_deadline(monkeypatch)
    harness = factory.make()
    harness.heartbeat.wake_up()
    harness.organs.gate = asyncio.Event()
    assert harness.heartbeat.submit(said("本轮不会等到模型"))
    harness.heartbeat.beat()
    await asyncio.wait_for(harness.heartbeat.wait_for_thought(), 1)
    harness.heartbeat.beat()
    harness.heartbeat.beat()

    [incoming] = harness.records(EVENT_MESSAGE)
    [interrupted] = harness.records(EVENT_PLANNING_INTERRUPTED)
    assert harness.organs.cancelled
    assert harness.sent == []
    assert harness.records(EVENT_REPLY_REFERENCE) == harness.records(EVENT_REPLY_DELIVERED) == []
    assert interrupted.content == "思考超时，本轮未完成。"
    assert interrupted.metadata["reason"] == "deadline"
    assert interrupted.metadata["round_id"] == incoming.id
    assert interrupted.visibility is incoming.visibility
    assert "planner_ms" in interrupted.metadata["stage_timings"]

    harness.organs.gate.set()
    await harness.exchange("下一轮恢复")
    assert [item.kind for item in harness.sent] == [MessageKind.REPLY]


@pytest.mark.asyncio
async def test_queued_segments_still_deliver(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    organs = ScriptedOrgans("第一段。<n>第二段。")
    harness = factory.make(organs, split_reply_enabled=True, reply_split_mode="explicit")
    harness.heartbeat.wake_up()
    await harness.exchange("第一轮能得到两段")
    assert [item.content for item in harness.sent] == ["第一段。"]

    shorten_deadline(monkeypatch)
    organs.gate = asyncio.Event()
    await harness.exchange("第二轮等不到结果")
    assert len(harness.records(EVENT_PLANNING_INTERRUPTED)) == 1
    assert len(harness.records(EVENT_REPLY_DELIVERED)) == 1
    harness.clock.advance(timedelta(seconds=60))
    harness.heartbeat.beat()
    assert [item.content for item in harness.sent] == ["第一段。", "第二段。"]
    assert len(harness.records(EVENT_REPLY_DELIVERED)) == 2


@pytest.mark.asyncio
async def test_deadline_disabled_by_zero(factory: Factory, monkeypatch: pytest.MonkeyPatch) -> None:
    shorten_deadline(monkeypatch)
    harness = factory.make(thought_deadline_seconds=0)
    harness.heartbeat.wake_up()
    gate = asyncio.Event()
    harness.organs.gate = gate
    assert harness.heartbeat.submit(said("单轮不设时限"))
    harness.heartbeat.beat()
    await asyncio.sleep(0.04)
    harness.heartbeat.beat()
    assert harness.sent == [] and not harness.organs.cancelled
    gate.set()
    await asyncio.wait_for(harness.heartbeat.wait_for_thought(), 1)
    harness.heartbeat.beat()
    assert harness.records(EVENT_PLANNING_INTERRUPTED) == []
    assert len(harness.records(EVENT_REPLY_DELIVERED)) == 1


@pytest.mark.asyncio
async def test_provider_timeout_is_not_the_round_deadline(factory: Factory) -> None:
    class WrongTiming(ScriptedOrgans):
        async def call(self, request: OrganRequest) -> OrganResponse:
            raise TimeoutError("假调用自身故障")

    harness = factory.make(WrongTiming())
    harness.heartbeat.wake_up()
    await harness.exchange("服务商自己的超时")
    assert harness.records(EVENT_PLANNING_INTERRUPTED) == []
    assert harness.records(EVENT_REPLY_DELIVERED) == []
