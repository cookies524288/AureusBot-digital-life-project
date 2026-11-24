"""用合成平台消息验证规划器实际只查本轮已读历史。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import Heartbeat, MessageRoute, Perception, TrustLevel
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.planner_tools import PlannerToolOptions, dispatch_planner_tools, planner_tools
from aureus.heartbeat.platform_history import HistoryBoundary, HistoryQuery, PlatformHistory
from aureus.heartbeat.recall import MAX_RECALL_CHARS
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse, ToolCall
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

NOW = datetime(2026, 11, 24, tzinfo=UTC)
KEY = ConversationKey("qq", "bot", "group", "target")
VISIBILITY = VisibilityScope.GROUP


def perception(key: ConversationKey, identifier: str, content: str) -> Perception:
    return Perception(
        "qq", "owner", content, VISIBILITY, TrustLevel.VERIFIED_USER,
        route=MessageRoute(key.account_id, key.kind, key.recipient_id, identifier,
                           key.temporary_group_id),
    )


def record_legacy(timeline: TimelineDatabase, key: ConversationKey, identifier: str, text: str) -> None:
    timeline.append("message", text, ActorType.PERSON, "member", visibility=VISIBILITY, metadata={
        "channel": key.platform, "account_id": key.account_id, "message_type": key.kind,
        "recipient_id": key.recipient_id, "temporary_group_id": key.temporary_group_id,
        "message_id": identifier,
    })


def test_history_uses_scoped_read_watermark_and_separate_legacy_cursor(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    clock = ManualClock(NOW)
    store, timeline = InboxStore(path, clock), TimelineDatabase(path, clock)
    timeline.open()
    try:
        for key in (ConversationKey("qq", "other", "group", "target"),
                    ConversationKey("qq", "bot", "group", "other"),
                    ConversationKey("qq", "bot", "private", "target", "other"), KEY):
            stored = store.add(perception(key, "same", f"keyword:{key.account_id}:{key.kind}:{key.recipient_id}"))
            assert stored is not None
            store.mark_read([stored.id])
            record_legacy(timeline, key, "same", "keyword:duplicate")
        record_legacy(timeline, KEY, "legacy", "legacy keyword")
        record_legacy(timeline, ConversationKey("qq", "bot", "private", "target"), "legacy", "other private")
        record_legacy(timeline, ConversationKey("qq", "", "group", "target"), "legacy", "unclassified keyword")
        unread = store.add(perception(KEY, "unread", "unread keyword"))
        assert unread is not None
        history = PlatformHistory(store, timeline.platform_history)
        first = json.loads(history.read(KEY, VISIBILITY, HistoryQuery(1, "keyword", None)))
        assert [item["content"] for item in first["messages"]] == ["keyword:bot:group:target"]
        second = json.loads(history.read(KEY, VISIBILITY, HistoryQuery(
            1, "keyword", HistoryBoundary(**first["next_before"]),
        )))
        assert second["messages"][0]["content"] == "legacy keyword"
        assert second["messages"][0]["platform_message_id"] == "legacy"
        assert second["next_before"] is None
        assert json.loads(history.read(KEY, VISIBILITY, HistoryQuery(20, "duplicate", None)))["status"] == "no_results"
        assert store.list_unread(KEY) == [unread]
        store.add_delivered(perception(KEY, "outgoing", '"' * 6000), sender_name="bot")
        encoded = history.read(KEY, VISIBILITY, HistoryQuery(20, None, None))
        assert len(encoded) <= MAX_RECALL_CHARS
        assert json.loads(encoded)["messages"][0]["direction"] == "outgoing"
    finally:
        timeline.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "revoked", "repeat", "failure", "foreign_route"])
async def test_planner_dispatch_reads_at_most_one_tool_and_does_not_fall_back(mode: str) -> None:
    tools = planner_tools(PlannerToolOptions(platform_history=True, recall=True))
    requests: list[OrganRequest] = []
    authorized = True

    async def call(request: OrganRequest) -> OrganResponse:
        requests.append(request)
        if not request.tool_history:
            arguments = {"account_id": "other"} if mode == "foreign_route" else {"limit": 8}
            return OrganResponse("planner", "", tool_calls=(ToolCall("read_platform_history", "first", arguments),))
        assert request.tool_history[0].results[0].content == '{"status":"no_results"}'
        followup = ToolCall("query_memory", "second", {"keywords": ["消息"]}) if mode == "repeat" else ToolCall("stop", "second", {})
        return OrganResponse("planner", "", tool_calls=(followup,))

    async def history(query: HistoryQuery) -> str:
        nonlocal authorized
        assert query.limit == 8
        if mode == "failure":
            raise OSError("synthetic")
        authorized = mode != "revoked"
        return '{"status":"no_results"}'

    async def recall(_words: tuple[str, ...]) -> None:
        pytest.fail("不准绕过共享查询上限")

    work = dispatch_planner_tools(OrganRequest("planner", ""), tools, call, recall,
                                  read_history=history, still_authorized=lambda: authorized)
    if mode == "failure":
        with pytest.raises(OSError, match="synthetic"):
            await work
    else:
        plan, _ = await work
        assert (plan is not None) == (mode == "success")
    assert len(requests) == (2 if mode in {"success", "repeat"} else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["read", "read_then_reply", "cleanup", "revoked_cleanup"])
async def test_actual_heartbeat_declares_scope_and_uses_tool_message(tmp_path: Path, action: str) -> None:
    path = tmp_path / "test.db"
    clock = ManualClock(NOW - timedelta(days=1))
    store, timeline = InboxStore(path, clock), TimelineDatabase(path, clock)
    timeline.open()
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    authorized = True
    reading = action.startswith("read")
    tool_name = "read_platform_history" if reading else "propose_platform_history_cleanup"

    class Organs:
        async def call(self, request: OrganRequest) -> OrganResponse:
            nonlocal authorized
            if request.organ_id == "replyer":
                assert action == "read_then_reply"
                return OrganResponse("replyer", "收到。")
            assert request.organ_id == "planner"
            assert tool_name in {tool.name for tool in request.tools}
            if request.tool_history:
                result = request.tool_history[0].results[0].content
                if reading:
                    assert "旧文只给工具" in result
                else:
                    assert json.loads(result)["originals"] == 1
                if action == "revoked_cleanup":
                    authorized = False
                assert "旧文只给工具" not in request.prompt
                if action == "read_then_reply":
                    return OrganResponse("planner", "", tool_calls=(ToolCall("reply", "reply", {
                        "addressed_to_me": "yes", "reply_reference": "旧文只给工具",
                        "intent": "旧文只给工具", "reason": "旧文只给工具",
                    }),))
                return OrganResponse("planner", "", tool_calls=(ToolCall("stop", "stop", {}),))
            arguments = {"keyword": "旧文"} if reading else {"before_date": "2026-11-24"}
            return OrganResponse("planner", "", tool_calls=(ToolCall(
                tool_name, action, arguments,
            ),))

    heart = Heartbeat(
        clock=clock, timeline=timeline, organs=Organs(),
        identity=Identity.model_validate({
            "facts": {"name": "测试", "birthday": "2026-11-24"}, "user": {"name": "维护者"},
            "persona": {"immutable": "温和。"},
        }),
        config=HeartbeatConfig(inbox_debounce_enabled=False),
        permissions=PermissionChecker(stop), emergency_stop=stop, inbox_store=store,
    )
    delivered = []
    heart.attach_channel("qq", delivered.append)
    heart.set_inbox_route_filter(lambda key: authorized and key == KEY)
    try:
        old = store.add(perception(KEY, "prior", "旧文只给工具"))
        clock.advance(timedelta(days=1))
        assert old is not None
        store.mark_read([old.id])
        heart.wake_up()
        heart.submit(perception(KEY, "now", "请查历史"))
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        logs = timeline.query(TimelineQuery(limit=50))
        assert any(tool_name in str(record.metadata) for record in logs)
        assert all("旧文只给工具" not in str(record.metadata) + record.content for record in logs)
        assert bool(delivered) == (action == "read_then_reply")
        assert heart.history_retention is not None
        assert (heart.history_retention.pending(KEY) is not None) == (action == "cleanup")
        assert store.get_visible_message(KEY, old.id) is not None
    finally:
        await heart.shut_down()
        timeline.close()
