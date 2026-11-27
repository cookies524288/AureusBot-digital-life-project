"""阶段计时基于单调时钟，且只在收到真实投递回执后记投递用时。"""

from datetime import timedelta

import pytest

from aureus.heartbeat import (
    EVENT_REPLY_REFERENCE,
    MessageKind,
    Perception,
    TrustLevel,
    round_timing,
)
from aureus.heartbeat.events import EVENT_REPLY_DELIVERED
from aureus.organs import OrganRequest, OrganResponse
from aureus.timeline import VisibilityScope
from tests.heartbeat.test_main_loop import Factory, ScriptedOrgans
from tests.heartbeat.test_thought_deadline import factory as factory


@pytest.mark.asyncio
async def test_stage_timings_present_and_monotonic(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monotonic = 0.0
    monkeypatch.setattr(round_timing, "monotonic", lambda: monotonic)

    class TimedOrgans(ScriptedOrgans):
        async def call(self, request: OrganRequest) -> OrganResponse:
            nonlocal monotonic
            if request.organ_id == "planner":
                monotonic += 2
            elif request.organ_id == "replyer":
                monotonic += 3
            return await super().call(request)

    harness = factory.make(
        TimedOrgans("第一段。<n>第二段。"), split_reply_enabled=True, reply_split_mode="explicit"
    )
    heartbeat = harness.heartbeat
    heartbeat.attach_channel("web", harness.sent.append)
    heartbeat.wake_up()
    assert heartbeat.submit(
        Perception(
            channel="web",
            sender_id="fake",
            content="收到后分两段回复。",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    heartbeat.beat()
    await heartbeat.wait_for_thought()
    heartbeat.beat()
    [reference] = harness.records(EVENT_REPLY_REFERENCE)
    before_delivery = reference.metadata["stage_timings"]
    assert before_delivery == {
        "inbox_wait_ms": 0,
        "debounce_ms": 0,
        "context_ms": 0,
        "planner_ms": 2000,
        "replyer_ms": 3000,
        "split_ms": 0,
    }
    [first] = harness.sent
    assert first.kind is MessageKind.REPLY and first.confirm_delivery is not None
    assert harness.records(EVENT_REPLY_DELIVERED) == []
    monotonic += 1
    first.confirm_delivery()
    first.confirm_delivery()
    [first_record] = harness.records(EVENT_REPLY_DELIVERED)
    assert first_record.metadata["stage_timings"] == {**before_delivery, "first_delivery_ms": 6000}

    harness.clock.advance(timedelta(seconds=3600))
    heartbeat.beat()
    second = harness.sent[-1]
    assert second.content == "第二段。" and second.confirm_delivery is not None
    monotonic += 0.5
    second.confirm_delivery()
    _, last_record = harness.records(EVENT_REPLY_DELIVERED)
    assert last_record.metadata["stage_timings"] == {
        **before_delivery,
        "first_delivery_ms": 6000,
        "last_delivery_ms": 6500,
    }
    assert harness.records(EVENT_REPLY_REFERENCE)[0].metadata["stage_timings"] == before_delivery


def test_round_wait_reports_only_debounce_window_not_later_queue_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(round_timing, "monotonic", lambda: 11)
    timing = round_timing.RoundTiming(queued_at=5, debounce_until=7)
    assert timing.metadata() == {"inbox_wait_ms": 6000, "debounce_ms": 2000}
