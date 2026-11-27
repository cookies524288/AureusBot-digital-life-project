"""网页实时推送的测试（3.43）：订阅、鉴权、只推标识、急停/停机暂停。

用 Starlette 的测试客户端，不开真实端口；心跳用假对象，往真的时间线里写一条再回一句，
不联网、不花钱。
"""

import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aureus.channels import WEB_CHANNEL_NAME, WEB_USER_ID, WebChannel
from aureus.common import ManualClock, WebConfig
from aureus.heartbeat import (
    EVENT_MESSAGE,
    MessageKind,
    OutgoingMessage,
    Perception,
)
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.live import (
    RESOURCE_LANGUAGE,
    TOPIC_INBOX,
    TOPIC_RESOURCES,
    TOPIC_ROUND,
    LiveEvent,
    LiveEventHub,
)
from aureus.timeline import ActorType, TimelineDatabase
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app

START = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
GOOD = "correct horse"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
SAME_ORIGIN = {"origin": "http://testserver"}


class RecordingHeartbeat:
    """网页说一句话：先记进时间线（模拟心跳收感知），再回一句聊天消息。"""

    def __init__(self, timeline: TimelineDatabase) -> None:
        self.timeline = timeline
        self.channel: WebChannel | None = None
        self.received: list[Perception] = []

    def submit(self, perception: Perception) -> bool:
        self.received.append(perception)
        self.timeline.append(
            EVENT_MESSAGE,
            perception.content,
            ActorType.USER,
            actor_id=perception.sender_id,
            visibility=perception.visibility,
        )
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
    def __init__(self, tmp_path: Path) -> None:
        self.clock = ManualClock(START)
        self.config = WebConfig()
        self.passwords = PasswordStore(tmp_path / "web_password.json", FAST_HASHER)
        self.passwords.set_password(GOOD)
        self.sessions = SessionStore(tmp_path / "aureus.db", self.clock, self.config.session_days)
        self.sessions.open()
        self.timeline = TimelineDatabase(tmp_path / "aureus.db", self.clock)
        self.timeline.open()
        self.live = LiveEventHub()
        self.allowed: set[ConversationKey] = set()
        self.heartbeat = RecordingHeartbeat(self.timeline)
        self.channel = WebChannel(self.heartbeat)
        self.heartbeat.channel = self.channel
        auth = WebAuth(self.passwords, self.sessions, LoginLimiter(self.config, self.clock))
        app = create_app(
            config=self.config,
            auth=auth,
            sessions=self.sessions,
            channel=self.channel,
            timeline=self.timeline,
            live_events=self.live,
            allows_inbox_conversation=self.allowed.__contains__,
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


def test_events_need_a_login(rig: Rig) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        rig.client.websocket_connect("/ws?events=1", headers=SAME_ORIGIN),
    ):
        pass


def test_a_connected_event_page_is_kicked_out_after_logging_out(rig: Rig) -> None:
    rig.log_in()
    with rig.client.websocket_connect("/ws?events=1", headers=SAME_ORIGIN) as socket:
        rig.sessions.revoke_all()
        socket.send_json({"text": "还能说话吗"})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_a_timeline_record_pushes_only_its_id_and_event_type(rig: Rig) -> None:
    rig.log_in()
    secret = "这句私聊正文不能出现在推送里"
    with rig.client.websocket_connect("/ws?events=1", headers=SAME_ORIGIN) as socket:
        socket.send_json({"text": secret})
        event = socket.receive_json()
        reply = socket.receive_json()

    assert event["type"] == "event"
    assert event["topic"] == "timeline"
    assert set(event["data"]) == {"id", "event_type"}
    assert event["data"]["event_type"] == EVENT_MESSAGE
    assert secret not in json.dumps(event, ensure_ascii=False)
    assert reply["content"] == "收到：" + secret


def test_a_page_without_the_events_parameter_only_gets_chat(rig: Rig) -> None:
    rig.log_in()
    with rig.client as client, client.websocket_connect("/ws", headers=SAME_ORIGIN) as socket:
        assert client.portal is not None
        client.portal.call(
            rig.live.publish, LiveEvent(TOPIC_RESOURCES, {"resource": RESOURCE_LANGUAGE})
        )
        socket.send_json({"text": "只收聊天"})
        message = socket.receive_json()

    assert message["type"] == "reply"
    assert message["content"] == "收到：只收聊天"


def test_a_paused_hub_stops_pushing_until_resumed(rig: Rig) -> None:
    rig.log_in()
    with (
        rig.client as client,
        client.websocket_connect("/ws?events=1", headers=SAME_ORIGIN) as socket,
    ):
        assert client.portal is not None
        rig.live.pause()
        client.portal.call(
            rig.live.publish, LiveEvent(TOPIC_RESOURCES, {"resource": RESOURCE_LANGUAGE})
        )
        socket.send_json({"text": "急停中"})
        assert socket.receive_json()["content"] == "收到：急停中"

        rig.live.resume()
        client.portal.call(
            rig.live.publish, LiveEvent(TOPIC_RESOURCES, {"resource": RESOURCE_LANGUAGE})
        )
        assert socket.receive_json() == {
            "type": "event",
            "topic": "resources",
            "data": {"resource": RESOURCE_LANGUAGE},
        }


def test_the_subscription_ends_when_the_page_disconnects(rig: Rig) -> None:
    rig.log_in()
    with rig.client.websocket_connect("/ws?events=1", headers=SAME_ORIGIN):
        assert rig.live.subscriber_count == 1
    assert rig.live.subscriber_count == 0
    assert rig.channel.subscriber_count == 0


def test_conversation_pushes_respect_permissions_at_send_time(rig: Rig) -> None:
    visible = ConversationKey("qq", "account", "private", "peer")
    denied = ConversationKey("qq", "account", "private", "peer", "other-group")
    rig.allowed.add(visible)
    rig.log_in()
    with (
        rig.client as client,
        client.websocket_connect("/ws?events=1", headers=SAME_ORIGIN) as socket,
    ):
        assert client.portal is not None
        for topic, key in ((TOPIC_INBOX, denied), (TOPIC_ROUND, denied), (TOPIC_INBOX, visible)):
            client.portal.call(
                rig.live.publish,
                LiveEvent(topic, {"active": True, "conversation": asdict(key)}),
            )
        assert socket.receive_json() == {
            "type": "event",
            "topic": "round",
            "data": {"active": False},
        }
        assert socket.receive_json()["data"]["conversation"] == asdict(visible)
