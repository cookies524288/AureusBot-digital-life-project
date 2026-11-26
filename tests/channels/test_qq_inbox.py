"""QQ 入站收件箱的持久化、范围与名称。"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from aureus.channels.qq import QQChannel
from aureus.common import ManualClock, QQConfig
from aureus.heartbeat import (
    MessageKind,
    MessageRoute,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
)
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, FakeOneBot, event


class Receiver:
    def submit(self, perception: Perception) -> bool:
        pytest.fail("持久化收件时，通道没有直接提交到旧心跳收件队列")


@pytest.mark.asyncio
async def test_qq_persists_allowed_conversations_before_notifying(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    path = tmp_path / "aureus.db"
    inbox = InboxStore(path, clock)
    notified: asyncio.Queue[InboxMessage] = asyncio.Queue()
    fake = FakeOneBot()
    async with asyncio.timeout(5), serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = QQConfig(
            enabled=True,
            account_id=str(ACCOUNT),
            allowed_group_id=str(GROUP),
            endpoint=f"ws://127.0.0.1:{port}/onebot",
        )
        async with QQChannel(
            config,
            None,
            Receiver(),
            lambda account: account == "2100",
            inbox_store=inbox,
            on_inbox_message=notified.put_nowait,
        ):
            remote = await fake.connections.get()
            group = event(31, 2200, "群消息", group=GROUP, sender={"card": "群内昵称"})
            await fake.send(remote, **event(32, 2200, "范围外", group=GROUP + 1))
            await fake.send(remote, **event(33, 2200, "范围外私聊"))
            await fake.send(remote, **group)
            await fake.send(remote, **group)
            await fake.send(remote, **event(34, 2100, "私聊", sender={"nickname": "测试用户"}))
            first = await notified.get()
            direct = await notified.get()
            while inbox.list_messages(first.conversation)[0].conversation_name != "合成群":
                await asyncio.sleep(0.01)
            assert notified.empty()
    assert first.sender_name == "群内昵称"
    assert first.timestamp == datetime.fromtimestamp(1_795_360_000, UTC)
    assert direct.perception.trust is TrustLevel.VERIFIED_USER
    assert direct.perception.visibility is VisibilityScope.USER_ONLY
    assert direct.conversation_name == "测试用户"
    reopened = InboxStore(path, clock)
    assert [item.last_message_id for item in reopened.list_conversations()] == [direct.id, first.id]
    assert reopened.add(first.perception) is None
    assert reopened.list_messages(first.conversation)[0].conversation_name == "合成群"
    assert reopened.list_priority_unread(direct.conversation, ("2100",)) == [direct]
    reopened.mark_read([direct.id])
    assert InboxStore(path, clock).list_unread(direct.conversation) == []
    assert reopened.list_unread(first.conversation)[0].id == first.id
    assert reopened.list_messages(first.conversation, before_id=first.id) == []
    other = ConversationKey("qq", "9999", "group", str(GROUP))
    assert reopened.list_messages(other) == []


@pytest.mark.asyncio
async def test_qq_marks_name_mentions_before_saving(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    path = tmp_path / "aureus.db"
    store = InboxStore(path, clock)
    notified: asyncio.Queue[InboxMessage] = asyncio.Queue()
    fake = FakeOneBot()
    async with asyncio.timeout(5), serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = QQConfig(
            enabled=True,
            account_id=str(ACCOUNT),
            endpoint=f"ws://127.0.0.1:{port}/onebot",
        )
        async with QQChannel(
            config,
            None,
            Receiver(),
            lambda account: account == "2100",
            inbox_store=store,
            on_inbox_message=notified.put_nowait,
            bot_names=lambda: ("Aureus Test", "小A"),
        ):
            remote = await fake.connections.get()
            await fake.send(remote, **event(41, 2200, "Aureus Test 在吗", group=GROUP))
            await fake.send(remote, **event(42, 2200, "小A 你好", group=GROUP))
            await fake.send(remote, **event(43, 2200, "普通消息", group=GROUP))
            await fake.send(
                remote,
                **event(
                    44,
                    2200,
                    "",
                    group=GROUP,
                    message=[
                        {"type": "at", "data": {"qq": str(ACCOUNT)}},
                        {"type": "text", "data": {"text": "点名"}},
                    ],
                ),
            )
            rows = [await notified.get() for _ in range(4)]
    assert [row.perception.mentioned for row in rows] == [True, True, False, True]
    reopened = InboxStore(path, clock)
    saved = {
        row.perception.content: row.perception.mentioned
        for row in reopened.list_messages(rows[0].conversation)
    }
    assert saved == {
        "@9000 点名": True,
        "普通消息": False,
        "小A 你好": True,
        "Aureus Test 在吗": True,
    }


@pytest.mark.asyncio
async def test_qq_history_only_accepts_platform_deliveries(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    path = tmp_path / "aureus.db"
    store = InboxStore(path, clock)
    fake = FakeOneBot()
    fake.reply_with = ["failed", "ok", "ok"]
    notified: asyncio.Queue[InboxMessage] = asyncio.Queue()
    async with asyncio.timeout(5), serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = QQConfig(
            enabled=True,
            account_id=str(ACCOUNT),
            endpoint=f"ws://127.0.0.1:{port}/onebot",
        )
        async with QQChannel(
            config,
            None,
            Receiver(),
            lambda _account: False,
            inbox_store=store,
            on_inbox_message=notified.put_nowait,
        ) as channel:
            remote = await fake.connections.get()
            await fake.send(remote, **event(31, 2200, "群消息", group=GROUP))
            await fake.send(remote, **event(31, 2200, "别的会话同键", group=GROUP + 1))
            inbound = await notified.get()
            assert (await notified.get()).conversation.recipient_id == str(GROUP + 1)
            route = MessageRoute(str(ACCOUNT), "group", str(GROUP), "31")
            for text in ("平台拒绝", "真正发送", "回执重复"):
                channel.show(
                    OutgoingMessage(
                        "qq",
                        "2200",
                        text,
                        MessageKind.REPLY,
                        "自己",
                        route=route,
                        visibility=VisibilityScope.GROUP,
                    )
                )
            await asyncio.gather(*channel._sending)
            await fake.send(remote, **event(32, 2200, "迟到", group=GROUP, time=1))
            late = await notified.get()
            assert late.timestamp < inbound.timestamp
    reopened = InboxStore(path, clock)
    history = reopened.list_messages(inbound.conversation)
    assert [(row.direction, row.perception.content) for row in history] == [
        ("incoming", "迟到"),
        ("outgoing", "真正发送"),
        ("incoming", "群消息"),
    ]
    assert history[1].perception.sender_id == str(ACCOUNT)
    assert history[1].perception.route == MessageRoute(str(ACCOUNT), "group", str(GROUP), "6000")
    assert [row.id for row in reopened.list_unread(inbound.conversation)] == [inbound.id, late.id]
