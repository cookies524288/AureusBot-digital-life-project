"""SnowLuma 结构化事件通过实际通道、回执和收件箱重放。"""

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import serve

from aureus.channels.qq import QQChannel
from aureus.common import ManualClock, QQConfig
from aureus.heartbeat import MessageKind, MessageRoute, OutgoingMessage, VisibilityScope
from aureus.heartbeat.inbox_store import InboxMessage, InboxStore
from tests.channels.test_qq_inbox import Receiver
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, FakeOneBot, event
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import NOW

OWNER = 2100
FRIEND = 2200
THIRD_PARTY = 2300


def segment(kind: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": data}


def poke(**fields: Any) -> dict[str, Any]:
    return {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "time": 1_795_360_000,
        "self_id": ACCOUNT,
        **fields,
    }


@pytest.mark.asyncio
async def test_snowluma_components_are_scoped_archived_and_replayed(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    path = tmp_path / "inbox.db"
    store = InboxStore(path, clock)
    fake = FakeOneBot()
    fake.reply_with = ["failed", "ok", "ok"]
    notified: asyncio.Queue[InboxMessage] = asyncio.Queue()
    async with asyncio.timeout(10), serve(fake.handle, "127.0.0.1", 0) as server:
        config = QQConfig(
            enabled=True,
            account_id=str(ACCOUNT),
            allowed_group_id=str(GROUP),
            endpoint=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/onebot",
        )
        async with QQChannel(
            config,
            None,
            Receiver(),
            lambda account: account == str(OWNER),
            inbox_store=store,
            on_inbox_message=notified.put_nowait,
        ) as channel:
            remote = await fake.connections.get()
            await fake.send(remote, **event(10, THIRD_PARTY, "原件", group=GROUP))
            await fake.send(remote, **event(11, OWNER, "同键的普通私聊"))
            source = await notified.get()
            private = await notified.get()
            await fake.send(
                remote,
                **event(
                    12,
                    FRIEND,
                    "",
                    group=GROUP,
                    message=[
                        segment("reply", id=10),
                        segment("text", text="第一段"),
                        segment("at", qq=ACCOUNT),
                        segment("image", file="image-token"),
                        segment("reply", id=11, qq=OWNER),
                        segment("at", qq="all"),
                        segment("text", text="最后一段"),
                    ],
                ),
            )
            mixed = await notified.get()
            for notice in (
                poke(
                    group_id=GROUP,
                    user_id=FRIEND,
                    target_id=THIRD_PARTY,
                    action="<img src=https://invalid.example/>",
                    suffix="肩膀",
                    action_img_url="https://invalid.example/personalized.png",
                ),
                poke(user_id=OWNER, sender_id=OWNER, target_id=ACCOUNT),
                poke(user_id=OWNER, sender_id=ACCOUNT, target_id=OWNER),
                poke(group_id=GROUP + 1, user_id=OWNER, target_id=ACCOUNT),
                poke(group_id=GROUP, user_id=OWNER, target_id=ACCOUNT, self_id=ACCOUNT + 1),
                poke(user_id=FRIEND, sender_id=FRIEND, target_id=ACCOUNT),
                poke(user_id=OWNER, sender_id=FRIEND, target_id=ACCOUNT),
                poke(group_id=0, user_id=OWNER, sender_id=OWNER, target_id=ACCOUNT),
            ):
                await fake.send(remote, **notice)
            await fake.send(
                remote,
                **event(
                    13,
                    FRIEND,
                    "",
                    group=GROUP,
                    message=[
                        segment("poke", type=42),
                    ],
                ),
            )
            await fake.send(remote, **event(14, OWNER, "同步屏障"))
            assert (await notified.get()).perception.content == "同步屏障"
            assert notified.empty() and fake.messages.empty()
            route = mixed.perception.route
            assert route is not None
            for text in ("桥拒绝", "已发回复", "重复回执"):
                channel.show(
                    OutgoingMessage(
                        "qq",
                        str(FRIEND),
                        text,
                        MessageKind.REPLY,
                        "自己",
                        route=route,
                        quote_source=True,
                        visibility=VisibilityScope.GROUP,
                    )
                )
            await asyncio.gather(*channel._sending)
            actual = [await fake.messages.get() for _ in range(3)]
            assert actual[1]["message"][0] == segment("reply", id="12")

    reopened = InboxStore(path, clock)
    assert reopened.add(mixed.perception) is None
    group_rows = list(reversed(reopened.list_messages(source.conversation)))
    assert [row.direction for row in group_rows] == [
        "incoming",
        "incoming",
        "notice",
        "archived",
        "outgoing",
    ]
    assert [row.id for row in reopened.list_unread(source.conversation)] == [source.id, mixed.id]
    assert [
        row.direction
        for row in reopened.summary_window(source.conversation, VisibilityScope.GROUP, 0, 50)
    ] == ["incoming", "incoming", "outgoing"]
    components = group_rows[1].perception.components
    assert [part.position for part in components] == list(range(7))
    assert [part.kind for part in components] == [
        "reply",
        "text",
        "at",
        "image",
        "reply",
        "at",
        "text",
    ]
    assert components[0].target_message_id == "10"
    assert components[0].target_account_id == str(THIRD_PARTY)
    assert components[2].target_account_id == str(ACCOUNT) and components[3].attachment_index == 0
    assert components[4].target_message_id == "11" and components[4].target_account_id is None
    assert components[5].all_members
    archived = group_rows[3].perception
    assert archived.components[0].poke_type == 42 and archived.quoted_message_id is None
    delivered = group_rows[4].perception
    assert delivered.content == "已发回复" and delivered.sender_id == str(ACCOUNT)
    assert delivered.route == MessageRoute(str(ACCOUNT), "group", str(GROUP), "6000")
    assert delivered.components[0].target_message_id == "12"
    assert delivered.components[0].target_account_id == str(FRIEND)
    assert set(reopened.visible_conversation_keys()) == {source.conversation, private.conversation}

    allowed = {source.conversation, private.conversation}
    rig = ConfigRig(tmp_path, inbox_store=reopened, allows_inbox_conversation=allowed.__contains__)
    try:
        rig.log_in()
        group_params = {
            key: value for key, value in asdict(source.conversation).items() if value is not None
        }
        group_params["page"] = "1"
        response = rig.client.get("/api/inbox/messages", params=group_params)
        assert response.status_code == 200
        rows = response.json()["messages"]
        notice = next(row for row in rows if row["event_type"] == "poke_notice")
        assert notice["sender_id"] == str(FRIEND) and notice["platform_message_id"] is None
        assert notice["components"][0]["target_account_id"] == str(THIRD_PARTY)
        assert notice["components"][0]["action"] == "<img src=https://invalid.example/>"
        assert "personalized.png" not in response.text and "image-token" not in response.text
        assert rows[0]["platform_message_id"] == "6000"
        private_params = {**group_params, "kind": "private", "recipient_id": str(OWNER)}
        private_response = rig.client.get("/api/inbox/messages", params=private_params)
        assert private_response.status_code == 200
        private_rows = private_response.json()["messages"]
        assert [
            (row["direction"], row["sender_id"], row["components"][0]["target_account_id"])
            for row in private_rows
            if row["event_type"] == "poke_notice"
        ] == [("outgoing", str(ACCOUNT), str(OWNER)), ("incoming", str(OWNER), str(ACCOUNT))]
        allowed.remove(source.conversation)
        assert rig.client.get("/api/inbox/messages", params=group_params).json()["messages"] == []
    finally:
        rig.close()
