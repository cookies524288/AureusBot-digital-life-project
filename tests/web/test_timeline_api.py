"""时间线和总览接口：假时钟、真数据库，不开网络端口（D84）。"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import get_ident

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from aureus import __version__
from aureus.channels import WebChannel
from aureus.common import ManualClock, WebConfig
from aureus.core import CognitiveState
from aureus.heartbeat import EVENT_REPLY_REFERENCE, Perception, recent_dialogue
from aureus.heartbeat.events import (
    EVENT_JUDGMENT,
    EVENT_RECALL_FEEDBACK,
    EVENT_RECALL_REPLY_DELIVERED,
    EVENT_REPLY_DELIVERED,
)
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.reply_records import INTERNAL_REPLY_RECORD
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)
GOOD = "correct horse"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class FakeHeartbeat:
    replying_conversation: ConversationKey | None = None
    replying_web = False

    def is_replying_to(
        self,
        *,
        conversation: ConversationKey | None = None,
        web_visibility: VisibilityScope | None = None,
    ) -> bool:
        if conversation is not None:
            return conversation == self.replying_conversation
        return self.replying_web and web_visibility is VisibilityScope.USER_ONLY

    def __init__(self) -> None:
        self.cognitive_state: CognitiveState | None = None
        self.is_thinking = False
        self.limit = 2
        self.maintenance_paused = False
        self.summaries: dict[str, str] = {}
        self.focus: tuple[int, int | None] | None = None
        self.capacity: int | None = None

    def set_maintenance_paused(self, paused: bool) -> None:
        self.maintenance_paused = paused

    def submit(self, perception: Perception) -> bool:
        return True

    def describe_state(self) -> str:
        return "测试状态"

    def context_message_limit(self) -> int:
        return self.limit

    def working_memory_summaries(self) -> dict[str, str]:
        return self.summaries

    def working_memory_focus(self) -> tuple[int, int | None] | None:
        return self.focus

    def working_memory_capacity(self) -> int | None:
        return self.capacity


class Rig:
    def __init__(
        self, tmp_path: Path, *, password_set: bool = True, recall_feedback_enabled: bool = True
    ) -> None:
        self.clock = ManualClock(NOW)
        self.heartbeat = FakeHeartbeat()
        self.channel = WebChannel(self.heartbeat)
        config = WebConfig()
        passwords = PasswordStore(tmp_path / "web_password.argon2", FAST_HASHER)
        if password_set:
            passwords.set_password(GOOD)
        self.sessions = SessionStore(tmp_path / "aureus.db", self.clock, config.session_days)
        self.sessions.open()
        self.timeline = TimelineDatabase(tmp_path / "aureus.db", self.clock)
        self.timeline.open()
        auth = WebAuth(passwords, self.sessions, LoginLimiter(config, self.clock))
        app = create_app(
            config=config,
            auth=auth,
            sessions=self.sessions,
            channel=self.channel,
            timeline=self.timeline,
            heartbeat=self.heartbeat,
            clock=self.clock,
            maintenance_paused=lambda: self.heartbeat.maintenance_paused,
            set_maintenance_paused=self.heartbeat.set_maintenance_paused,
            recall_feedback_enabled=recall_feedback_enabled,
        )
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def log_in(self, password: str = GOOD) -> None:
        assert self.client.post("/api/login", json={"password": password}).status_code == 200

    def close(self) -> None:
        self.sessions.close()
        self.timeline.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


@pytest.mark.parametrize("path", ["/api/timeline", "/api/overview"])
def test_timeline_routes_require_login_and_a_changed_password(tmp_path: Path, path: str) -> None:
    rig = Rig(tmp_path, password_set=False)
    try:
        assert rig.client.get(path).status_code == 401
        rig.log_in("admin")
        assert rig.client.get(path).status_code == 403
    finally:
        rig.close()


def test_recall_feedback_is_bound_to_a_delivered_reply_and_is_reversible(rig: Rig) -> None:
    visibility = VisibilityScope.GROUP
    evidence = rig.timeline.append("message", "合成依据", ActorType.USER, visibility=visibility)
    reference = f"timeline:{evidence.id}"
    reply = rig.timeline.append(
        "message",
        "合成回复",
        ActorType.ME,
        visibility=visibility,
        metadata={"recall_feedback_refs": [reference, "source:sample"]},
    )
    old = rig.timeline.append(
        "message",
        "旧回复",
        ActorType.ME,
        visibility=visibility,
        metadata={"recall_record_ids": [evidence.id]},
    )
    private = rig.timeline.append(
        "message",
        "私有记录",
        ActorType.ME,
        metadata={"recall_feedback_refs": [reference]},
    )
    path = f"/api/timeline/{reply.id}/recall-feedback"
    origin = {"origin": "http://testserver"}
    key = sha256(reference.encode()).hexdigest()
    payload = {"evidence_key": key, "rating": "useful"}
    assert rig.client.get(path).status_code == 401
    assert rig.client.patch(path, json=payload, headers=origin).status_code == 401
    rig.log_in()
    assert rig.client.get(path).status_code == 404
    assert rig.client.patch(path, json=payload, headers=origin).status_code == 404
    for target in (reply, old, private):
        rig.timeline.append(
            EVENT_RECALL_REPLY_DELIVERED,
            "合成回执",
            ActorType.SYSTEM,
            visibility=target.visibility,
            metadata={"reply_id": target.id, "segment": 1},
        )
    for target in (old, private):
        rejected = f"/api/timeline/{target.id}/recall-feedback"
        assert rig.client.get(rejected).status_code == 404
        assert rig.client.patch(rejected, json=payload, headers=origin).status_code == 404
    assert rig.client.patch(path, json=payload).status_code == 403
    unrelated = sha256(b"timeline:9999").hexdigest()
    assert (
        rig.client.patch(
            path,
            json={**payload, "evidence_key": unrelated},
            headers=origin,
        ).status_code
        == 404
    )
    assert rig.client.get(path).json() == [
        {"key": key, "label": f"时间线记录 #{evidence.id}", "rating": None},
        {
            "key": sha256(b"source:sample").hexdigest(),
            "label": f"记忆依据 · 编号 {sha256(b'sample').hexdigest()[:12]}",
            "rating": None,
        },
    ]
    for rating in (None, "useful", "useful", "not_useful", None, None):
        result = rig.client.patch(path, json={**payload, "rating": rating}, headers=origin)
        assert result.json() == {"rating": rating}
        assert rig.client.get(path).json()[0]["rating"] == rating
    history = rig.timeline.query(TimelineQuery(event_type=EVENT_RECALL_FEEDBACK))
    assert [record.metadata["rating"] for record in history] == ["useful", "not_useful", None]
    assert all(
        record.visibility is visibility and record.actor_type is ActorType.USER
        for record in history
    )
    assert rig.timeline.get(reply.id) == reply and rig.timeline.get(evidence.id) == evidence
    records = rig.client.get("/api/timeline").json()["records"]
    assert [item["id"] for item in records if item.get("recall_feedback_available")] == [reply.id]
    assert all("recall_feedback_refs" not in item["metadata"] for item in records)


def test_recall_feedback_switch_hides_controls_and_rejects_mutation(tmp_path: Path) -> None:
    rig = Rig(tmp_path, recall_feedback_enabled=False)
    try:
        visibility = VisibilityScope.USER_ONLY
        reply = rig.timeline.append(
            "message",
            "已有评价的回复",
            ActorType.ME,
            visibility=visibility,
            metadata={"recall_feedback_refs": ["timeline:42"]},
        )
        rig.timeline.append(
            EVENT_RECALL_REPLY_DELIVERED,
            "合成回执",
            ActorType.SYSTEM,
            visibility=visibility,
            metadata={"reply_id": reply.id, "segment": 1},
        )
        rig.log_in()
        path = f"/api/timeline/{reply.id}/recall-feedback"
        assert rig.client.get(path).status_code == 404
        assert (
            rig.client.patch(
                path,
                headers={"origin": "http://testserver"},
                json={"evidence_key": sha256(b"timeline:42").hexdigest(), "rating": "not_useful"},
            ).status_code
            == 404
        )
        [record] = rig.client.get("/api/timeline?types=message").json()["records"]
        assert "recall_feedback_available" not in record
    finally:
        rig.close()


@pytest.mark.parametrize("path", ["/api/history", "/api/timeline", "/api/overview"])
def test_hot_timeline_reads_leave_the_request_thread(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    database_threads: list[int] = []
    original_query = rig.timeline.query

    def track_query(query: TimelineQuery) -> list[TimelineRecord]:
        database_threads.append(get_ident())
        return original_query(query)

    monkeypatch.setattr(rig.timeline, "query", track_query)
    with rig.client as client:
        rig.log_in()
        assert client.portal is not None
        request_thread = client.portal.call(get_ident)
        assert client.get(path).status_code == 200
    assert database_threads and all(thread != request_thread for thread in database_threads)


def test_timeline_filters_time_and_paginates_in_ascending_order(rig: Rig) -> None:
    rig.log_in()
    rig.timeline.append("message", "旧记录", ActorType.USER)
    rig.clock.advance(timedelta(hours=25))
    for _ in range(3):
        rig.timeline.append("message", "新记录", ActorType.USER)
    first = rig.client.get("/api/timeline?limit=2").json()
    assert [record["id"] for record in first["records"]] == [3, 4]
    assert first["next_before_id"] == 3
    second = rig.client.get("/api/timeline?limit=2&before_id=3").json()
    assert [record["id"] for record in second["records"]] == [2]
    assert second["next_before_id"] is None
    assert rig.client.get("/api/timeline?hours=26").json()["records"][0]["id"] == 1


def test_reply_pages_show_only_confirmed_segments_and_never_infer_legacy_rounds(rig: Rig) -> None:
    legacy = rig.timeline.append("message", "未标投递的历史回复", ActorType.ME)
    first = rig.timeline.append("message", "来源一", ActorType.USER)
    second = rig.timeline.append("message", "来源二", ActorType.USER)
    rig.timeline.append(
        "message",
        "确认段\n未确认段",
        ActorType.ME,
        metadata={"round_id": first.id, INTERNAL_REPLY_RECORD: True},
    )
    for round_id, text in ((first.id, "确认段"), (second.id, "另一轮"), (first.id, "后续确认段")):
        rig.timeline.append(
            EVENT_REPLY_DELIVERED,
            text,
            ActorType.ME,
            metadata={"round_id": round_id},
        )
    for _ in range(4):
        rig.timeline.append("message", "入站消息", ActorType.USER)

    path = "/api/timeline?types=reply_delivered&limit=2"
    assert rig.client.get(path).status_code == 401
    rig.log_in()
    latest = rig.client.get(path).json()
    previous = rig.client.get(f"{path}&before_id={latest['next_before_id']}").json()
    combined = previous["records"] + latest["records"]
    assert [record["round_id"] for record in combined] == [first.id, second.id, first.id]
    assert [record["content"] for record in combined] == ["确认段", "另一轮", "后续确认段"]
    all_records = rig.client.get("/api/timeline").json()["records"]
    assert all("未确认段" not in record["content"] for record in all_records)
    assert "round_id" not in next(record for record in all_records if record["id"] == legacy.id)


def test_power_requires_ready_and_same_origin_and_keeps_web_available(rig: Rig) -> None:
    origin = {"Origin": "http://testserver"}
    assert rig.client.post("/api/power/stop", headers=origin).status_code == 401
    rig.log_in()
    assert rig.client.post("/api/power/stop").status_code == 403
    assert rig.client.post("/api/power/stop", headers=origin).json() == {"maintenance_paused": True}
    assert rig.client.get("/api/overview").json()["maintenance_paused"] is True
    assert rig.client.get("/api/timeline").status_code == 200
    assert rig.client.post("/api/power/start", headers=origin).json() == {
        "maintenance_paused": False
    }


def test_conversation_filter_keeps_only_that_chat_and_its_round_events(rig: Rig) -> None:
    rig.log_in()
    conversation = {
        "platform": "qq",
        "account_id": "10000",
        "kind": "group",
        "recipient_id": "20000",
    }
    chat_metadata = {
        "channel": "qq",
        "account_id": "10000",
        "message_type": "group",
        "recipient_id": "20000",
    }
    first = rig.timeline.append("message", "群里的来信", ActorType.USER, metadata=chat_metadata)
    other = rig.timeline.append(
        "message", "别的群来信", ActorType.USER, metadata={**chat_metadata, "recipient_id": "30000"}
    )
    rig.timeline.append(
        EVENT_JUDGMENT,
        "判断了刚收到的消息里的情绪。",
        ActorType.ME,
        metadata={"round_id": first.id, "in_reply_to": [first.id]},
    )
    rig.timeline.append(
        EVENT_REPLY_REFERENCE,
        "建议回复：打招呼",
        ActorType.ME,
        metadata={
            **chat_metadata,
            "round_id": first.id,
            "in_reply_to": [first.id],
            "actions": [{"action": "reply", "status": "completed", "reference": "回复参考"}],
        },
    )
    reply = rig.timeline.append(
        "message",
        "回复正文",
        ActorType.ME,
        metadata={**chat_metadata, "round_id": first.id, INTERNAL_REPLY_RECORD: True},
    )
    rig.timeline.append(
        EVENT_REPLY_DELIVERED,
        "回复正文",
        ActorType.ME,
        metadata={"channel": "qq", "round_id": first.id, "reply_id": reply.id, "segment": 1},
    )
    rig.timeline.append(
        EVENT_JUDGMENT, "别的群的判断", ActorType.ME, metadata={"round_id": other.id}
    )
    records = rig.client.get(
        "/api/timeline", params={"conversation": json.dumps(conversation)}
    ).json()["records"]
    assert [record["content"] for record in records] == [
        "群里的来信",
        "判断了刚收到的消息里的情绪。",
        "建议回复：打招呼",
        "回复正文",
    ]
    assert [record.get("round_id") for record in records] == [None, first.id, first.id, first.id]
    assert all("别的群" not in record["content"] for record in records)
    unfiltered = rig.client.get("/api/timeline").json()["records"]
    assert [record["content"] for record in unfiltered] == [
        "群里的来信",
        "别的群来信",
        "判断了刚收到的消息里的情绪。",
        "建议回复：打招呼",
        "回复正文",
        "别的群的判断",
    ]


@pytest.mark.parametrize(
    "value", ["不是 JSON", "[]", '{"platform":"qq"}', '{"platform":"qq","account_id":""}']
)
def test_conversation_filter_rejects_malformed_values(rig: Rig, value: str) -> None:
    rig.log_in()
    response = rig.client.get("/api/timeline", params={"conversation": value})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_types_filter_and_private_metadata_are_returned_without_mutation(rig: Rig) -> None:
    rig.log_in()
    metadata = {"channel": "terminal", "nested": {"items": [1, True]}}
    rig.timeline.append("thought", "想法", ActorType.ME, metadata=metadata)
    rig.timeline.append("message", "对话", ActorType.USER)
    rig.timeline.append("diary_written", "日记", ActorType.ME)
    data = rig.client.get("/api/timeline?types=thought,,diary_written,thought&limit=2").json()
    assert [record["id"] for record in data["records"]] == [1, 3]
    assert data["records"][0]["visibility"] == "private"
    assert data["records"][0]["metadata"] == metadata
    assert data["records"][0]["channel"] == "terminal"
    assert data["records"][1]["channel"] == ""
    assert data["summary"] == {}
    assert rig.timeline.count_records() == 3


def test_context_boundary_uses_the_actual_reply_query_and_ignores_page_filters(rig: Rig) -> None:
    rig.log_in()
    for scope in (
        VisibilityScope.USER_ONLY,
        VisibilityScope.GROUP,
        VisibilityScope.USER_ONLY,
        VisibilityScope.USER_ONLY,
        VisibilityScope.PRIVATE,
    ):
        rig.timeline.append("message", "对话", ActorType.USER, visibility=scope)
    records = recent_dialogue(
        rig.timeline,
        visibility=rig.channel.user_visibility,
        limit=rig.heartbeat.context_message_limit(),
    )
    data = rig.client.get("/api/timeline?before_id=2&limit=1&types=thought").json()
    assert data["context"] == {"message_limit": 2, "boundary_id": records[0].id}
    assert records[0].id == 3
    assert data["records"] == []


def test_empty_timeline_has_no_boundary_or_next_page(rig: Rig) -> None:
    rig.log_in()
    data = rig.client.get("/api/timeline").json()
    assert data == {
        "summary": {},
        "records": [],
        "next_before_id": None,
        "context": {"message_limit": 2, "boundary_id": None},
    }


def test_timeline_returns_latest_working_memory_summary_to_signed_in_user(rig: Rig) -> None:
    rig.heartbeat.summaries = {"用户私聊": "已保存的摘要"}
    assert rig.client.get("/api/timeline").status_code == 401
    rig.log_in()
    result = rig.client.get("/api/timeline?types=message").json()
    assert result["records"] == []
    assert result["summary"] == {"用户私聊": "已保存的摘要"}


@pytest.mark.parametrize(("hours", "expected"), [(0, [2]), (9999, [1, 2])])
def test_hours_are_clamped(rig: Rig, hours: int, expected: list[int]) -> None:
    rig.log_in()
    rig.timeline.append("message", "旧", ActorType.USER)
    rig.clock.advance(timedelta(hours=2))
    rig.timeline.append("message", "新", ActorType.USER)
    data = rig.client.get(f"/api/timeline?hours={hours}&before_id=-1").json()
    assert [record["id"] for record in data["records"]] == expected


@pytest.mark.parametrize(("limit", "expected"), [(0, 1), (9999, 300)])
def test_limits_are_clamped(rig: Rig, limit: int, expected: int) -> None:
    rig.log_in()
    for _ in range(301):
        rig.timeline.append("message", "对话", ActorType.USER)
    data = rig.client.get(f"/api/timeline?limit={limit}").json()
    assert len(data["records"]) == expected
    assert data["next_before_id"] == 302 - expected


@pytest.mark.parametrize("query", ["hours=no", "limit=no", "before_id=no"])
def test_invalid_query_returns_a_chinese_string(rig: Rig, query: str) -> None:
    rig.log_in()
    response = rig.client.get(f"/api/timeline?{query}")
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert "整数" in response.json()["detail"]


def test_overview_before_and_after_the_first_beat(rig: Rig) -> None:
    rig.log_in()
    assert rig.client.get("/api/overview").json() == {
        "version": __version__,
        "state_text": "测试状态",
        "thinking": False,
        "energy_level": None,
        "energy_tier": "",
        "current_task": "",
        "working_memory_capacity": None,
        "reply_reference": None,
        "restart_required": False,
        "maintenance_paused": False,
    }
    rig.heartbeat.cognitive_state = CognitiveState(
        energy_level=0.2, energy_state="低电量", current_task="回复用户"
    )
    rig.heartbeat.is_thinking = True
    data = rig.client.get("/api/overview").json()
    assert data["energy_level"] == 0.2
    assert data["energy_tier"] == "低电量"
    assert data["current_task"] == "回复用户"
    assert data["thinking"] is True


def test_overview_reports_configured_capacity_and_latest_reference(rig: Rig) -> None:
    rig.heartbeat.limit = 10
    rig.heartbeat.capacity = 84
    rig.timeline.append(EVENT_REPLY_REFERENCE, "上一轮建议", ActorType.ME)
    newest = rig.timeline.append(
        EVENT_REPLY_REFERENCE,
        "建议不回复\n依据：只是在道别",
        ActorType.ME,
        visibility=VisibilityScope.GROUP,
        metadata={"channel": "qq", "recipient_id": "fake-group"},
    )
    rig.timeline.append("message", "不相关的消息", ActorType.USER)
    assert rig.client.get("/api/overview").status_code == 401
    rig.log_in()
    data = rig.client.get("/api/overview").json()
    assert data["working_memory_capacity"] == 84
    assert data["reply_reference"] == {
        "content": newest.content,
        "time": newest.timestamp.isoformat(),
        "visibility": "group",
        "channel": "qq",
    }
    assert not recent_dialogue(rig.timeline, visibility=VisibilityScope.GROUP, limit=10)
