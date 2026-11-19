"""网页通道的测试：用假心跳，不碰网络（路线图 1.10，D63 第 8 条）。"""

import pytest

from aureus.channels import WEB_CHANNEL_NAME, WEB_USER_ID, WebChannel
from aureus.channels.web import MAX_MESSAGE_CHARS
from aureus.heartbeat import MessageKind, OutgoingMessage, Perception, TrustLevel, VisibilityScope


class FakeHeartbeat:
    def __init__(self, accepting: bool = True) -> None:
        self.accepting = accepting
        self.received: list[Perception] = []

    def submit(self, perception: Perception) -> bool:
        self.received.append(perception)
        return self.accepting


def make_message(content: str = "你好") -> OutgoingMessage:
    return OutgoingMessage(
        channel=WEB_CHANNEL_NAME,
        recipient_id=WEB_USER_ID,
        content=content,
        kind=MessageKind.REPLY,
        sender_name="小绿",
    )


def test_a_message_from_the_web_user_becomes_a_verified_private_perception() -> None:
    heartbeat = FakeHeartbeat()
    channel = WebChannel(heartbeat)

    assert channel.send_from_user("  今天面试被拒了  ") is True

    assert heartbeat.received == [
        Perception(
            channel="web",
            sender_id="local",
            content="今天面试被拒了",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    ]


def test_it_reports_when_the_heartbeat_is_not_accepting() -> None:
    channel = WebChannel(FakeHeartbeat(accepting=False))
    assert channel.send_from_user("在吗") is False


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_an_empty_message_is_refused_before_reaching_the_heartbeat(blank: str) -> None:
    heartbeat = FakeHeartbeat()
    with pytest.raises(ValueError, match="空的"):
        WebChannel(heartbeat).send_from_user(blank)
    assert heartbeat.received == []


def test_the_length_limit_is_exact() -> None:
    heartbeat = FakeHeartbeat()
    channel = WebChannel(heartbeat)
    assert channel.send_from_user("字" * MAX_MESSAGE_CHARS) is True
    with pytest.raises(ValueError, match="太长"):
        channel.send_from_user("字" * (MAX_MESSAGE_CHARS + 1))
    assert len(heartbeat.received) == 1


def test_messages_from_the_heartbeat_reach_every_subscriber() -> None:
    channel = WebChannel(FakeHeartbeat())
    phone: list[OutgoingMessage] = []
    laptop: list[OutgoingMessage] = []
    channel.subscribe(phone.append)
    channel.subscribe(laptop.append)
    message = make_message()

    channel.show(message)

    assert phone == [message]
    assert laptop == [message]
    assert channel.subscriber_count == 2


def test_unsubscribing_stops_delivery_and_is_repeatable() -> None:
    channel = WebChannel(FakeHeartbeat())
    kept: list[OutgoingMessage] = []
    gone: list[OutgoingMessage] = []
    channel.subscribe(kept.append)
    unsubscribe = channel.subscribe(gone.append)

    unsubscribe()
    unsubscribe()
    channel.show(make_message())

    assert len(kept) == 1
    assert gone == []
    assert channel.subscriber_count == 1


def test_a_failing_subscriber_does_not_stop_the_others_or_the_caller() -> None:
    channel = WebChannel(FakeHeartbeat())
    received: list[OutgoingMessage] = []

    def broken(_message: OutgoingMessage) -> None:
        raise RuntimeError("连接断了")

    channel.subscribe(broken)
    channel.subscribe(received.append)

    channel.show(make_message())  # 不抛出

    assert len(received) == 1


def test_a_subscriber_may_unsubscribe_while_receiving() -> None:
    channel = WebChannel(FakeHeartbeat())
    received: list[OutgoingMessage] = []
    holder: list[object] = []

    def once(message: OutgoingMessage) -> None:
        received.append(message)
        unsubscribe()

    unsubscribe = channel.subscribe(once)
    holder.append(unsubscribe)
    channel.subscribe(received.append)

    channel.show(make_message())
    channel.show(make_message("第二条"))

    assert [m.content for m in received] == ["你好", "你好", "第二条"]


def test_showing_with_nobody_connected_is_harmless() -> None:
    WebChannel(FakeHeartbeat()).show(make_message())
