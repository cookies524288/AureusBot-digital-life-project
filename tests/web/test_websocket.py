# WebSocket 测试（路线图 1.10，D63 第 8 条）：握手检查、收发、出错不断开、登录失效被踢下线。
# 心跳用回声假心跳：收到话就在同一个事件循环里回一条，避免跨线程碰队列。

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aureus.channels import WEB_CHANNEL_NAME, WEB_USER_ID, WebChannel
from aureus.common import ManualClock, WebConfig
from aureus.heartbeat import MessageKind, OutgoingMessage, Perception
from aureus.timeline import TimelineDatabase
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
GOOD = "correct horse"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
SAME_ORIGIN = {"origin": "http://testserver"}


class EchoHeartbeat:
    def __init__(self) -> None:
        self.received: list[Perception] = []
        self.channel: WebChannel | None = None

    def submit(self, perception: Perception) -> bool:
        self.received.append(perception)
        if self.channel is not None:
            self.channel.show(
                OutgoingMessage(
                    channel=WEB_CHANNEL_NAME,
                    recipient_id=WEB_USER_ID,
                    content="收到：" + perception.content,
                    kind=MessageKind.REPLY,
                    sender_name="小绿",
                )
            )
        return True


class Rig:
    def __init__(self, tmp_path: Path, *, password_set: bool = True) -> None:
        clock = ManualClock(START)
        config = WebConfig()
        self.passwords = PasswordStore(tmp_path / "web_password.argon2", FAST_HASHER)
        if password_set:
            self.passwords.set_password(GOOD)
        self.sessions = SessionStore(tmp_path / "aureus.db", clock, config.session_days)
        self.sessions.open()
        self.timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
        self.timeline.open()
        self.heartbeat = EchoHeartbeat()
        self.channel = WebChannel(self.heartbeat)
        self.heartbeat.channel = self.channel
        auth = WebAuth(self.passwords, self.sessions, LoginLimiter(config, clock))
        app = create_app(
            config=config,
            auth=auth,
            sessions=self.sessions,
            channel=self.channel,
            timeline=self.timeline,
        )
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def log_in(self, password: str = GOOD) -> None:
        response = self.client.post("/api/login", json={"password": password})
        assert response.status_code == 200

    def close(self) -> None:
        self.sessions.close()
        self.timeline.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


def test_a_connection_without_login_is_refused(rig: Rig) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        rig.client.websocket_connect("/ws", headers=SAME_ORIGIN),
    ):
        pass


def test_a_connection_from_another_site_is_refused_even_when_logged_in(rig: Rig) -> None:
    rig.log_in()
    evil = {"origin": "http://evil.example"}
    with pytest.raises(WebSocketDisconnect), rig.client.websocket_connect("/ws", headers=evil):
        pass


def test_a_connection_without_an_origin_is_refused(rig: Rig) -> None:
    rig.log_in()
    with pytest.raises(WebSocketDisconnect), rig.client.websocket_connect("/ws"):
        pass


def test_a_connection_is_refused_after_logging_out(rig: Rig) -> None:
    rig.log_in()
    rig.client.post("/api/logout")
    with (
        pytest.raises(WebSocketDisconnect),
        rig.client.websocket_connect("/ws", headers=SAME_ORIGIN),
    ):
        pass


def test_a_connection_is_refused_while_the_password_is_the_default(tmp_path: Path) -> None:
    rig = Rig(tmp_path, password_set=False)
    try:
        rig.log_in("admin")
        with (
            pytest.raises(WebSocketDisconnect),
            rig.client.websocket_connect("/ws", headers=SAME_ORIGIN),
        ):
            pass
    finally:
        rig.close()


def test_what_the_user_says_reaches_the_heartbeat_and_the_reply_comes_back(rig: Rig) -> None:
    rig.log_in()
    with rig.client.websocket_connect("/ws", headers=SAME_ORIGIN) as socket:
        socket.send_json({"text": "你好"})
        assert socket.receive_json() == {
            "type": "reply",
            "sender": "小绿",
            "content": "收到：你好",
            "channel": "web",
        }
    assert [p.content for p in rig.heartbeat.received] == ["你好"]
    assert rig.heartbeat.received[0].sender_id == WEB_USER_ID


@pytest.mark.parametrize("bad", ["不是 JSON", "{}", "[1]", '{"text": 5}'])
def test_a_malformed_message_gets_an_error_and_the_connection_stays_open(
    rig: Rig, bad: str
) -> None:
    rig.log_in()
    with rig.client.websocket_connect("/ws", headers=SAME_ORIGIN) as socket:
        socket.send_text(bad)
        assert socket.receive_json()["type"] == "error"
        socket.send_json({"text": "还在"})
        assert socket.receive_json()["content"] == "收到：还在"
    assert [p.content for p in rig.heartbeat.received] == ["还在"]


def test_empty_and_overlong_messages_are_errors_not_disconnects(rig: Rig) -> None:
    rig.log_in()
    with rig.client.websocket_connect("/ws", headers=SAME_ORIGIN) as socket:
        socket.send_json({"text": "   "})
        assert "空的" in socket.receive_json()["detail"]
        socket.send_json({"text": "字" * 5000})
        assert "太长" in socket.receive_json()["detail"]
    assert rig.heartbeat.received == []


def test_the_subscription_ends_when_the_page_disconnects(rig: Rig) -> None:
    rig.log_in()
    with rig.client.websocket_connect("/ws", headers=SAME_ORIGIN):
        assert rig.channel.subscriber_count == 1
    assert rig.channel.subscriber_count == 0


def test_a_connected_page_is_kicked_out_when_every_device_is_logged_out(rig: Rig) -> None:
    rig.log_in()
    with rig.client.websocket_connect("/ws", headers=SAME_ORIGIN) as socket:
        rig.sessions.revoke_all()
        socket.send_json({"text": "还能说话吗"})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert rig.heartbeat.received == []
