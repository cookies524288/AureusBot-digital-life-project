"""工作记忆的滚动、隔离、恢复及心跳接入。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.attention import WorkingMemory, route_scope, summarize
from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import Heartbeat, Perception, TrustLevel
from aureus.identity import Identity
from aureus.organs import ModelPreference, OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope

NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


def _append(timeline: TimelineDatabase, text: str) -> None:
    timeline.append("message", text, ActorType.USER, visibility=VisibilityScope.USER_ONLY)


async def _summary(request: OrganRequest) -> OrganResponse:
    content = json.loads(request.prompt)["经历"][0]["content"]
    return OrganResponse(request.organ_id, f"摘要：{content}")


@pytest.mark.asyncio
async def test_default_window_rolls_and_restores(tmp_path: Path) -> None:
    path = tmp_path / "aureus.db"
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    _append(timeline, "迁移历史")
    memory = WorkingMemory(timeline, HeartbeatConfig())
    memory.restore()
    for index in range(79):
        _append(timeline, str(index))
    assert memory.prepare(clock.now(), ModelPreference.PREFERRED) is None
    _append(timeline, "79")
    first = memory.prepare(clock.now(), ModelPreference.PREFERRED)
    assert first is not None
    material = json.loads(first.requests[0][1].prompt)
    assert material["旧摘要"] == ""
    assert [entry["content"] for entry in material["经历"]] == list(map(str, range(55)))
    memory.finish(await summarize(first, _summary), clock.now())
    data = json.loads(memory.context(owner=True, route=None, exclude=()).split("\n", 1)[1])
    assert data["滚动摘要"] == ["摘要：0"]
    assert [entry["content"] for entry in data["近期焦点"]] == list(map(str, range(55, 80)))
    timeline.close()

    timeline = TimelineDatabase(path, clock)
    timeline.open()
    try:
        restored = WorkingMemory(timeline, HeartbeatConfig())
        restored.restore()
        assert restored.summaries() == {"用户私聊": "摘要：0"}
        for index in range(80, 135):
            _append(timeline, str(index))
        second = restored.prepare(clock.now(), ModelPreference.PREFERRED)
        assert second is not None
        material = json.loads(second.requests[0][1].prompt)
        assert material["旧摘要"] == "摘要：0"
        assert [entry["content"] for entry in material["经历"]] == list(map(str, range(55, 110)))
    finally:
        timeline.close()


@pytest.mark.asyncio
async def test_scope_isolation_and_failed_batch_retry(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
    timeline.open()
    memory = WorkingMemory(
        timeline, HeartbeatConfig(working_memory_capacity=5, working_memory_retain=1)
    )
    memory.restore()
    routes = (
        (VisibilityScope.GROUP, "qq", "group", "甲群"),
        (VisibilityScope.PRIVATE, "qq", "private", "私聊"),
        (VisibilityScope.GROUP, "other", "group", "甲群"),
    )
    for visibility, channel, kind, recipient in routes:
        timeline.append(
            "message",
            f"{channel}:{recipient}",
            ActorType.PERSON,
            visibility=visibility,
            metadata={
                "channel": channel,
                "account_id": "bot",
                "message_type": kind,
                "recipient_id": recipient,
            },
        )
    _append(timeline, "用户的私事")
    _append(timeline, "保留的焦点")

    async def invalid(request: OrganRequest) -> OrganResponse:
        return OrganResponse(request.organ_id, "")

    try:
        batch = memory.prepare(clock.now(), ModelPreference.PREFERRED)
        assert batch is not None and len(batch.requests) == 4
        memory.finish(await summarize(batch, invalid), clock.now())
        assert memory.summaries() == {}
        assert memory.prepare(clock.now(), ModelPreference.PREFERRED) is None
        clock.advance(timedelta(minutes=5))
        assert memory.prepare(clock.now(), ModelPreference.PREFERRED) == batch
        memory.finish(await summarize(batch, _summary), clock.now())
        for visibility, channel, kind, recipient in routes:
            route = route_scope(visibility, channel, "bot", kind, recipient, None)
            material = memory.context(owner=False, route=route, exclude=()).split("\n", 1)[1]
            assert json.loads(material)["滚动摘要"] == [f"摘要：{channel}:{recipient}"]
        assert "用户的私事" in memory.context(owner=True, route=None, exclude=())
        assert memory.context(owner=False, route=None, exclude=()) == ""
    finally:
        timeline.close()


class _Organs:
    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if request.organ_id == "summarizer":
            self.started.set()
            await self.release.wait()
            return await _summary(request)
        if request.organ_id == "planner":
            return OrganResponse(request.organ_id, '{"reply":true,"intent":"回应","length":"短"}')
        return OrganResponse(request.organ_id, "收到。")


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_actual_planner_and_replyer_get_saved_summary(
    tmp_path: Path, interrupt: bool
) -> None:
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
    timeline.open()
    config = HeartbeatConfig(working_memory_capacity=3, working_memory_retain=1)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = _Organs()
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        config=config,
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        identity=Identity.model_validate(
            {
                "facts": {"name": "小豆", "birthday": "2026-11-23"},
                "user": {"name": "主人"},
                "persona": {"immutable": "温和。"},
            }
        ),
        working_memory=WorkingMemory(timeline, config),
    )
    heart.attach_channel("web", lambda _: None)
    heart.update_available_organs(frozenset({"summarizer", "planner", "replyer"}))
    heart.wake_up()
    try:
        _append(timeline, "已压缩的对话")
        _append(timeline, "保留焦点的对话")
        heart.beat()
        await asyncio.wait_for(organs.started.wait(), 1)
        heart.beat()
        assert heart.working_memory_summaries() == {}
        if not interrupt:
            organs.release.set()
            await heart.wait_for_thought()
            heart.beat()
            assert heart.working_memory_summaries()["用户私聊"] == "摘要：已压缩的对话"
        assert heart.submit(
            Perception("web", "user", "请回我", VisibilityScope.USER_ONLY, TrustLevel.VERIFIED_USER)
        )
        heart.beat()
        await heart.wait_for_thought()
        if interrupt:
            heart.beat()
            await heart.wait_for_thought()
            assert heart.working_memory_summaries() == {}
        requests = [req for req in organs.requests if req.organ_id in {"planner", "replyer"}]
        assert {req.organ_id for req in requests} == {"planner", "replyer"}
        assert all(("摘要：已压缩的对话" in req.prompt) is not interrupt for req in requests)
    finally:
        organs.release.set()
        await heart.shut_down()
        timeline.close()
