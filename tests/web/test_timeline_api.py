"""时间线和总览接口：假时钟、真数据库，不开网络端口（D84）。"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from aureus import __version__
from aureus.channels import WebChannel
from aureus.common import ManualClock, WebConfig
from aureus.core import CognitiveState
from aureus.heartbeat import Perception, recent_dialogue
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)
GOOD = "correct horse"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class FakeHeartbeat:
    def __init__(self) -> None:
        self.cognitive_state: CognitiveState | None = None
        self.is_thinking = False
        self.limit = 2
        self.maintenance_paused = False
        self.summaries: dict[str, str] = {}
        self.focus: tuple[int, int | None] | None = None

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


class Rig:
    def __init__(self, tmp_path: Path, *, password_set: bool = True) -> None:
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
