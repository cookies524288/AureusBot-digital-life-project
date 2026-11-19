"""网页服务端的测试：登录、默认口令限制、历史对话（路线图 1.10，D63）。

用 Starlette 的测试客户端，不开真实端口。测试客户端的来源地址是 testclient，
不是回环地址，所以要测「本机」的路径时，把客户端的来源地址指定成 127.0.0.1。
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.common import ManualClock, WebConfig
from aureus.heartbeat import EVENT_MESSAGE, Perception
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app
from aureus.web.server import SESSION_COOKIE

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
GOOD = "correct horse"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class FakeHeartbeat:
    def submit(self, perception: Perception) -> bool:
        return True


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = ManualClock(START)
        self.config = WebConfig(login_max_failures=3)
        self.passwords = PasswordStore(tmp_path / "web_password.argon2", FAST_HASHER)
        self.sessions = SessionStore(tmp_path / "aureus.db", self.clock, self.config.session_days)
        self.sessions.open()
        self.timeline = TimelineDatabase(tmp_path / "aureus.db", self.clock)
        self.timeline.open()
        self.auth = WebAuth(self.passwords, self.sessions, LoginLimiter(self.config, self.clock))
        self.app = create_app(
            config=self.config,
            auth=self.auth,
            sessions=self.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=self.timeline,
        )

    def client(self, host: str = "127.0.0.1") -> TestClient:
        return TestClient(self.app, client=(host, 50000))

    def close(self) -> None:
        self.sessions.close()
        self.timeline.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


def login(client: TestClient, password: str = "admin") -> Any:
    return client.post("/api/login", json={"password": password})


def set_password(rig: Rig) -> None:
    rig.passwords.set_password(GOOD)


# ---------- 没登录什么都看不到 ----------


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/status"), ("get", "/api/history"), ("post", "/api/logout")],
)
def test_nothing_is_visible_without_logging_in(rig: Rig, method: str, path: str) -> None:
    response = getattr(rig.client(), method)(path)
    assert response.status_code == 401


def test_a_forged_cookie_is_not_a_login(rig: Rig) -> None:
    client = rig.client()
    client.cookies.set(SESSION_COOKIE, "x" * 43)
    assert client.get("/api/status").status_code == 401


def test_the_api_docs_are_not_exposed(rig: Rig) -> None:
    client = rig.client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ---------- 登录 ----------


def test_default_password_logs_in_from_this_machine_and_says_to_change_it(rig: Rig) -> None:
    client = rig.client()
    response = login(client)
    assert response.status_code == 200
    assert response.json() == {"must_change_password": True}
    assert client.get("/api/status").json() == {"must_change_password": True}


def test_the_cookie_is_http_only_and_same_site_strict(rig: Rig) -> None:
    header = login(rig.client()).headers["set-cookie"].lower()
    assert header.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in header
    assert "samesite=strict" in header
    assert f"max-age={7 * 86400}" in header


def test_default_password_is_refused_from_the_network(rig: Rig) -> None:
    response = login(rig.client("192.168.1.20"))
    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies


def test_a_wrong_password_is_401_without_echoing_it(rig: Rig) -> None:
    set_password(rig)
    response = login(rig.client(), "SECRET-guess")
    assert response.status_code == 401
    assert "SECRET" not in response.text


def test_too_many_failures_give_429_with_retry_after(rig: Rig) -> None:
    set_password(rig)
    client = rig.client("192.168.1.20")
    for _ in range(3):
        assert login(client, "wrong password").status_code == 401
    locked = login(client, GOOD)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) >= 1


def test_a_long_password_is_refused_before_hashing(rig: Rig) -> None:
    assert login(rig.client(), "x" * 1000).status_code == 422


def test_unknown_fields_in_the_login_body_are_refused(rig: Rig) -> None:
    response = rig.client().post("/api/login", json={"password": "admin", "extra": 1})
    assert response.status_code == 422


# ---------- 默认口令状态：只能改口令 ----------


def test_history_is_blocked_while_the_password_is_still_the_default(rig: Rig) -> None:
    client = rig.client()
    login(client)
    response = client.get("/api/history")
    assert response.status_code == 403
    assert "默认口令" in response.json()["detail"]


def test_changing_the_password_works_in_the_default_state(rig: Rig) -> None:
    client = rig.client()
    login(client)
    response = client.post("/api/password", json={"old_password": "admin", "new_password": GOOD})
    assert response.status_code == 200
    assert rig.auth.password_is_default is False


def test_changing_the_password_logs_this_device_out_too(rig: Rig) -> None:
    client = rig.client()
    login(client)
    client.post("/api/password", json={"old_password": "admin", "new_password": GOOD})
    assert client.get("/api/status").status_code == 401
    assert login(client, GOOD).json() == {"must_change_password": False}


@pytest.mark.parametrize("new", ["admin", "short", "x" * 300])
def test_a_bad_new_password_is_refused_and_nothing_changes(rig: Rig, new: str) -> None:
    client = rig.client()
    login(client)
    response = client.post("/api/password", json={"old_password": "admin", "new_password": new})
    assert response.status_code == 422
    assert rig.auth.password_is_default is True
    assert client.get("/api/status").status_code == 200  # 登录状态还在


def test_a_wrong_old_password_is_refused(rig: Rig) -> None:
    client = rig.client()
    login(client)
    response = client.post(
        "/api/password", json={"old_password": "not the old one", "new_password": GOOD}
    )
    assert response.status_code == 401
    assert rig.auth.password_is_default is True


# ---------- 退出 ----------


def test_logout_ends_only_this_device(rig: Rig) -> None:
    set_password(rig)
    phone, laptop = rig.client(), rig.client()
    login(phone, GOOD)
    login(laptop, GOOD)
    assert phone.post("/api/logout").status_code == 200
    assert phone.get("/api/status").status_code == 401
    assert laptop.get("/api/status").status_code == 200


# ---------- 历史对话 ----------


def add_message(rig: Rig, content: str, actor: ActorType, channel: str) -> None:
    rig.timeline.append(
        event_type=EVENT_MESSAGE,
        content=content,
        actor_type=actor,
        visibility=VisibilityScope.USER_ONLY,
        metadata={"channel": channel},
    )


def test_history_shows_every_channel_and_marks_where_each_came_from(rig: Rig) -> None:
    set_password(rig)
    add_message(rig, "终端上说的", ActorType.USER, "terminal")
    add_message(rig, "回给终端", ActorType.ME, "terminal")
    add_message(rig, "网页上说的", ActorType.USER, "web")
    client = rig.client()
    login(client, GOOD)

    items = client.get("/api/history").json()

    assert [(i["who"], i["channel"], i["content"]) for i in items] == [
        ("user", "terminal", "终端上说的"),
        ("me", "terminal", "回给终端"),
        ("user", "web", "网页上说的"),
    ]


def test_history_only_contains_messages(rig: Rig) -> None:
    set_password(rig)
    rig.timeline.append(
        event_type="cognitive_state",
        content="内部状态",
        actor_type=ActorType.ME,
        visibility=VisibilityScope.PRIVATE,
    )
    client = rig.client()
    login(client, GOOD)
    assert client.get("/api/history").json() == []


def test_history_limit_is_bounded_and_keeps_the_latest(rig: Rig) -> None:
    set_password(rig)
    for number in range(5):
        add_message(rig, f"第{number}条", ActorType.USER, "web")
    client = rig.client()
    login(client, GOOD)

    assert [i["content"] for i in client.get("/api/history?limit=2").json()] == [
        "第3条",
        "第4条",
    ]
    assert len(client.get("/api/history?limit=0").json()) == 1  # 下限 1
    assert len(client.get("/api/history?limit=100000").json()) == 5  # 上限 200，不报错
