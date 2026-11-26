"""工作记忆的滚动、隔离、恢复及心跳接入。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from aureus.attention import WorkingMemory, route_scope, summarize
from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import Heartbeat, Perception, TrustLevel
from aureus.identity import Identity
from aureus.organs import ModelPreference, OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope
from aureus.web.timeline_api import register_timeline_routes
from tests.prompt_assertions import dialogue_text

NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


def _append(timeline: TimelineDatabase, text: str) -> None:
    timeline.append("message", text, ActorType.USER, visibility=VisibilityScope.USER_ONLY)


async def _summary(request: OrganRequest) -> OrganResponse:
    assert request.organ_id == "utils"
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
    for index in range(49):
        _append(timeline, str(index))
    assert memory.prepare(clock.now(), ModelPreference.PREFERRED) is None
    _append(timeline, "49")
    first = memory.prepare(clock.now(), ModelPreference.PREFERRED)
    assert first is not None
    material = json.loads(first.requests[0][1].prompt)
    assert material["旧摘要"] == ""
    assert [entry["content"] for entry in material["经历"]] == list(map(str, range(30)))
    memory.finish(await summarize(first, _summary), clock.now())
    data = json.loads(memory.context(owner=True, route=None, exclude=()).split("\n", 1)[1])
    assert data["滚动摘要"] == ["摘要：0"]
    assert [entry["content"] for entry in data["近期焦点"]] == list(map(str, range(30, 50)))
    timeline.close()

    timeline = TimelineDatabase(path, clock)
    timeline.open()
    try:
        restored = WorkingMemory(timeline, HeartbeatConfig())
        restored.restore()
        assert restored.summaries() == {"用户私聊": "摘要：0"}
        for index in range(50, 80):
            _append(timeline, str(index))
        second = restored.prepare(clock.now(), ModelPreference.PREFERRED)
        assert second is not None
        material = json.loads(second.requests[0][1].prompt)
        assert material["旧摘要"] == "摘要：0"
        assert [entry["content"] for entry in material["经历"]] == list(map(str, range(30, 60)))
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
        if json.loads(request.prompt)["经历"][0]["content"] == "用户的私事":
            return OrganResponse(request.organ_id, " \n ")
        return await _summary(request)

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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["", "摘" * 501, None], ids=["empty", "too-long", "failed"])
async def test_legacy_summary_is_preserved_until_success(
    tmp_path: Path, invalid: str | None
) -> None:
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
    timeline.open()
    old = "旧" * 1200
    prior = timeline.append(
        "working_memory_checkpoint",
        "已有摘要",
        ActorType.ME,
        metadata={"through": 0, "summaries": {"用户私聊": old}},
    )
    memory = WorkingMemory(
        timeline, HeartbeatConfig(working_memory_capacity=2, working_memory_retain=1)
    )
    memory.restore()
    _append(timeline, "本轮新约定")
    _append(timeline, "保留的焦点")

    async def invalid_result(request: OrganRequest) -> OrganResponse:
        if invalid is None:
            raise RuntimeError("模拟器官失败")
        return OrganResponse(request.organ_id, invalid)

    async def at_limit(request: OrganRequest) -> OrganResponse:
        return OrganResponse(request.organ_id, f" {'新' * 500}\n")

    try:
        batch = memory.prepare(clock.now(), ModelPreference.PREFERRED)
        assert batch is not None
        assert json.loads(batch.requests[0][1].prompt)["旧摘要"] == old
        memory.finish(await summarize(batch, invalid_result), clock.now())
        assert memory.summaries() == {"用户私聊": old}
        assert old in memory.context(owner=True, route=None, exclude=())
        query = TimelineQuery(event_type="working_memory_checkpoint", latest=True, limit=1)
        assert timeline.query(query)[0].id == prior.id
        clock.advance(timedelta(minutes=5))
        assert memory.prepare(clock.now(), ModelPreference.PREFERRED) == batch
        memory.finish(await summarize(batch, at_limit), clock.now())
        assert memory.summaries() == {"用户私聊": "新" * 500}
        assert timeline.query(query)[0].metadata["through"] == batch.through
    finally:
        timeline.close()


class _Organs:
    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if request.context.get("task") == "working_memory_summary":
            assert request.organ_id == "utils"
            self.started.set()
            await self.release.wait()
            return await _summary(request)
        if request.organ_id == "planner":
            from tests.planner_fixtures import plan_response

            return plan_response(request, {"reply": True, "intent": "回应", "length": "短"})
        return OrganResponse(request.organ_id, "收到。")


def _heartbeat(
    tmp_path: Path,
    clock: ManualClock,
    timeline: TimelineDatabase,
    config: HeartbeatConfig,
    organs: _Organs,
) -> Heartbeat:
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        config=config,
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {"name": "主人"},
                "persona": {"immutable": "温和。"},
            }
        ),
        working_memory=WorkingMemory(timeline, config),
    )
    heart.attach_channel("web", lambda _: None)
    return heart


async def _assert_timeline_focus(
    heart: Heartbeat, timeline: TimelineDatabase, clock: ManualClock, boundary_id: int
) -> None:
    app = FastAPI()

    async def signed_in() -> str:
        return "test-session"

    register_timeline_routes(
        app,
        timeline=timeline,
        heartbeat=heart,
        clock=clock,
        visibility=VisibilityScope.USER_ONLY,
        require_ready=signed_in,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for params in ({}, {"limit": 1, "before_id": boundary_id, "types": "message"}):
            response = await client.get("/api/timeline", params=params)
            assert response.status_code == 200
            data = response.json()
            assert data["context"] == {
                "message_limit": 5,
                "boundary_id": boundary_id,
                "working_memory": True,
            }
            assert data["summary"]["用户私聊"] == "摘要：已压缩的对话"


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_actual_planner_and_replyer_get_saved_summary(
    tmp_path: Path, interrupt: bool
) -> None:
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
    timeline.open()
    config = HeartbeatConfig(working_memory_capacity=5, working_memory_retain=1)
    organs = _Organs()
    heart = _heartbeat(tmp_path, clock, timeline, config, organs)
    heart.update_available_organs(frozenset({"summarizer", "planner", "replyer"}))
    heart.wake_up()
    try:
        for text in ("已压缩的对话", "第一段旧对话", "第二段旧对话"):
            _append(timeline, text)
        _append(timeline, "保留焦点的对话")
        boundary_id = timeline.get_latest_id()
        heart.beat()
        await asyncio.sleep(0)
        assert not organs.requests
        heart.update_available_organs(frozenset({"utils", "summarizer", "planner", "replyer"}))
        heart.beat()
        await asyncio.wait_for(organs.started.wait(), 1)
        heart.beat()
        assert heart.working_memory_summaries() == {}
        if not interrupt:
            organs.release.set()
            await heart.wait_for_thought()
            heart.beat()
            assert heart.working_memory_summaries()["用户私聊"] == "摘要：已压缩的对话"
            await _assert_timeline_focus(heart, timeline, clock, boundary_id)
            await heart.shut_down()
            timeline.close()
            timeline.open()
            heart = _heartbeat(tmp_path, clock, timeline, config, organs)
            heart.update_available_organs(frozenset({"planner", "replyer"}))
            heart.wake_up()
            await _assert_timeline_focus(heart, timeline, clock, boundary_id)
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
        assert all(
            ("摘要：已压缩的对话" in dialogue_text(req)) is not interrupt for req in requests
        )
        if not interrupt:
            for request in requests:
                text = dialogue_text(request)
                assert "第一段旧对话" not in text
                assert "第二段旧对话" not in text
                assert text.count("保留焦点的对话") == 1
    finally:
        organs.release.set()
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_pending_replies_above_working_memory_capacity_drop_the_oldest(
    tmp_path: Path,
) -> None:
    """D146：每轮只把最新 capacity 条放进上下文，更早的丢弃出上下文，不排队到下一轮。"""
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
    timeline.open()
    config = HeartbeatConfig(working_memory_capacity=2, working_memory_retain=1)
    organs = _Organs()
    heart = _heartbeat(tmp_path, clock, timeline, config, organs)
    heart.update_available_organs(frozenset({"planner", "replyer"}))
    heart.wake_up()
    messages = [f"排队来信{index:02d}" for index in range(5)]
    try:
        for message in messages:
            assert heart.submit(
                Perception(
                    "web", "user", message, VisibilityScope.USER_ONLY, TrustLevel.VERIFIED_USER
                )
            )
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        await heart.wait_for_thought()
        records = timeline.query(TimelineQuery(event_type="message"))
        message_ids = [record.id for record in records if record.content in messages]
        assert len(message_ids) == 5  # 丢弃只针对上下文，时间线仍保留全部来信
        expected = [message_ids[-2:]]
        for organ_id in ("planner", "replyer"):
            requests = [request for request in organs.requests if request.organ_id == organ_id]
            assert [request.context["in_reply_to"] for request in requests] == expected
            for request, batch in zip(requests, expected, strict=True):
                text = dialogue_text(request)
                assert all(record.content in text for record in records if record.id in batch)
                assert sum(message in text for message in messages) == 2
                assert all(message not in text for message in messages[:-2])
        # 更早的 3 条已经丢弃出上下文，下一拍不会重新开始一轮。
        heart.beat()
        await heart.wait_for_thought()
        assert len([request for request in organs.requests if request.organ_id == "planner"]) == 1
        assert len([request for request in organs.requests if request.organ_id == "replyer"]) == 1
    finally:
        await heart.shut_down()
        timeline.close()


@pytest.mark.asyncio
async def test_context_keeps_only_the_latest_capacity_messages(tmp_path: Path) -> None:
    """D146：一批 60 条、容量 50 时，只把最新 50 条放进本轮，更早 10 条丢弃且不留在队列。"""
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
    timeline.open()
    config = HeartbeatConfig(working_memory_capacity=50, working_memory_retain=20)
    organs = _Organs()
    heart = _heartbeat(tmp_path, clock, timeline, config, organs)
    heart.update_available_organs(frozenset({"planner", "replyer"}))
    heart.wake_up()
    messages = [f"积压来信{index:02d}" for index in range(60)]
    try:
        for message in messages:
            assert heart.submit(
                Perception(
                    "web", "user", message, VisibilityScope.USER_ONLY, TrustLevel.VERIFIED_USER
                )
            )
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        await heart.wait_for_thought()
        records = timeline.query(TimelineQuery(event_type="message"))
        message_ids = [record.id for record in records if record.content in messages]
        assert len(message_ids) == 60  # 丢弃不删除原件：时间线仍有全部 60 条
        expected = [message_ids[-50:]]
        for organ_id in ("planner", "replyer"):
            requests = [request for request in organs.requests if request.organ_id == organ_id]
            assert [request.context["in_reply_to"] for request in requests] == expected
            for request in requests:
                text = dialogue_text(request)
                assert all(message in text for message in messages[-50:])
                assert all(message not in text for message in messages[:10])
        # 更早的 10 条不在下一轮队列里：再跳一拍没有新的规划。
        heart.beat()
        await heart.wait_for_thought()
        assert len([request for request in organs.requests if request.organ_id == "planner"]) == 1
        assert len([request for request in organs.requests if request.organ_id == "replyer"]) == 1
    finally:
        await heart.shut_down()
        timeline.close()
