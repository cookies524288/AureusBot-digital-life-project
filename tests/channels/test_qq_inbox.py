"""QQ 入站收件箱的持久化、范围与名称。"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from aureus.channels.qq import QQChannel
from aureus.common import ManualClock, QQConfig
from aureus.heartbeat import Perception, TrustLevel, VisibilityScope
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
