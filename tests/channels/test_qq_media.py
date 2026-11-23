"""以模拟 SnowLuma 验证附件持久化、读取和识别。"""

import asyncio
import json
from base64 import b64encode
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from aureus.channels.onebot import decode_message
from aureus.channels.qq import QQChannel
from aureus.common import ManualClock, QQConfig
from aureus.common.sqlite import open_connection
from aureus.heartbeat import MediaReference, MessageRoute
from aureus.heartbeat.inbox_media import InboxMedia
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.memory import EmojiStore
from aureus.organs import ModelPreference, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError
from aureus.timeline import ActorType, TimelineRecord
from tests.channels.test_qq_inbox import Receiver
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, event

IMAGE = b"\x89PNG\r\n\x1a\nfake-image"
SPEECH = b"ID3fake-audio"


def segment(kind: str, file_id: str) -> dict[str, Any]:
    return {"type": kind, "data": {"file": file_id, "url": "http://example.invalid/bad"}}


@pytest.mark.asyncio
async def test_qq_collects_marked_emoji_and_preserves_moderation(tmp_path: Path) -> None:
    now = datetime(2026, 11, 23, tzinfo=UTC)
    clock = ManualClock(now)
    store = EmojiStore(tmp_path / "emoji.db", tmp_path, clock)
    store.open()
    inbox = InboxStore(tmp_path / "inbox.db", clock)
    source = decode_message(
        event(
            50,
            2100,
            "",
            message=[
                {"type": "image", "data": {"file": "one.png", "sub_type": 1}},
                {"type": "image", "data": {"file": "two.png", "sub_type": 4, "summary": "[图片]"}},
            ],
        ),
        str(ACCOUNT),
    )
    assert source is not None
    persisted = inbox.add(source)
    assert persisted is not None
    [message] = inbox.list_unread(persisted.conversation)
    assert message.perception.attachments == (
        MediaReference("image", "one.png", True),
        MediaReference("image", "two.png"),
    )

    async def read(_source: object, attachment: MediaReference) -> tuple[bytes, str]:
        return IMAGE + attachment.file_id.encode(), "image/png"

    rejected = True

    async def recognize(request: OrganRequest) -> OrganResponse:
        if rejected:
            raise CallRejectedError(request.organ_id, "测试拒绝")
        return OrganResponse(request.organ_id, "打招呼的笑脸")

    flow = InboxMedia(recognize, store)
    flow.read = read
    flow.available = frozenset({"vlm"})
    record = TimelineRecord(
        id=1,
        timestamp=now,
        actor_type=ActorType.PERSON,
        event_type="message",
        content=source.content,
        visibility=source.visibility,
    )
    await flow.enrich([record], [message.perception], ModelPreference.PREFERRED)
    [entry] = store.browse("", 0, 10)
    assert entry.collection_count == 1 and not entry.is_registered

    rejected = False
    await flow.enrich([record], [message.perception], ModelPreference.PREFERRED)
    [entry] = store.candidates()
    assert entry.description == "打招呼的笑脸" and entry.collection_count == 2
    assert entry.digest is not None
    store.set_status(entry.digest, is_registered=False, is_banned=True)
    await flow.enrich([record], [message.perception], ModelPreference.PREFERRED)
    assert not store.candidates()
    [entry] = store.browse("", 0, 10)
    assert entry.collection_count == 3 and entry.is_banned and not entry.is_registered


@pytest.mark.asyncio
async def test_qq_reads_saved_media_on_demand(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []
    connected: asyncio.Queue[ServerConnection] = asyncio.Queue()
    received: asyncio.Queue[InboxMessage] = asyncio.Queue()

    async def handle(remote: ServerConnection) -> None:
        async for frame in remote:
            request = json.loads(frame)
            action = request["action"]
            envelope = {"status": "ok", "retcode": 0, "echo": request["echo"]}
            if action == "get_login_info":
                await remote.send(json.dumps({**envelope, "data": {"user_id": ACCOUNT}}))
                connected.put_nowait(remote)
                continue
            requests.append(request)
            if action == "get_record":
                encoded = b64encode(SPEECH).decode()
                await remote.send(json.dumps({**envelope, "data": {"base64": encoded}}))
                continue
            assert action == "download_file_image_stream"
            parts = (IMAGE[:9], IMAGE[9:])
            packets = [
                {"type": "stream", "data_type": "file_info", "file_size": len(IMAGE)},
                *[
                    {
                        "type": "stream",
                        "data_type": "file_chunk",
                        "index": index,
                        "data": b64encode(data).decode(),
                        "size": len(data),
                    }
                    for index, data in enumerate(parts)
                ],
                {
                    "type": "response",
                    "data_type": "file_complete",
                    "total_bytes": len(IMAGE),
                    "total_chunks": len(parts),
                },
            ]
            if request["params"]["file_id"] == "broken.png":
                packets[1]["index"] = 5
            for packet in packets:
                await remote.send(
                    json.dumps({**envelope, "stream": "stream-action", "data": packet})
                )

    now = datetime(2026, 11, 23, tzinfo=UTC)
    clock = ManualClock(now)
    path = tmp_path / "inbox.db"
    store = InboxStore(path, clock)
    sent_to_organs = []

    async def recognize(request: OrganRequest) -> OrganResponse:
        sent_to_organs.append((request.organ_id, request.media_bytes, request.media_type))
        text = {"vlm": "图片里的字", "voice": "语音里的话"}[request.organ_id]
        return OrganResponse(request.organ_id, text)

    async with asyncio.timeout(5), serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = QQConfig(enabled=True, account_id=str(ACCOUNT), endpoint=f"ws://127.0.0.1:{port}")
        async with QQChannel(
            config,
            None,
            Receiver(),
            lambda _account: False,
            inbox_store=store,
            on_inbox_message=received.put_nowait,
        ) as channel:
            remote = await connected.get()
            await remote.send(json.dumps(event(45, 2100, "", message=[segment("image", "ok.png")])))
            await remote.send(
                json.dumps(
                    event(
                        46,
                        2100,
                        "",
                        message=[
                            {"type": "text", "data": {"text": "原文"}},
                            segment("image", "broken.png"),
                            segment("image", "http://example.invalid/bad"),
                            segment("record", "voice.amr"),
                        ],
                    )
                )
            )
            pure, mixed = await received.get(), await received.get()
            assert requests == []
            reopened = InboxStore(path, clock)
            sources = [item.perception for item in reopened.list_unread(pure.conversation)]
            assert sources[0].route == MessageRoute(str(ACCOUNT), "private", "2100", "45")
            assert sources[0].content == "[图片]"
            assert sources[0].attachments == (MediaReference("image", "ok.png"),)
            assert sources[1] == mixed.perception
            assert sources[1].attachments == (
                MediaReference("image", "broken.png"),
                MediaReference("record", "voice.amr"),
            )
            records = [
                TimelineRecord(
                    id=index,
                    timestamp=now,
                    actor_type=ActorType.PERSON,
                    event_type="message",
                    content=source.content,
                    visibility=source.visibility,
                )
                for index, source in enumerate(sources, 1)
            ]
            flow = InboxMedia(recognize)
            flow.read = channel.read_inbox_attachment
            flow.available = frozenset({"vlm", "voice"})
            enriched = await flow.enrich(records, sources, ModelPreference.PREFERRED)
    assert [record.content for record in enriched] == [
        "[图片]\n[图片识别] 图片里的字",
        "原文\n[语音转写] 语音里的话",
    ]
    assert [record.content for record in records] == ["[图片]", "原文"]
    assert sent_to_organs == [("vlm", IMAGE, "image/png"), ("voice", SPEECH, "audio/mpeg")]
    assert [request["params"]["file_id"] for request in requests] == [
        "ok.png",
        "broken.png",
        "voice.amr",
    ]
    assert requests[-1]["params"] == {"file_id": "voice.amr", "out_format": "mp3"}


def test_previous_inbox_schema_keeps_text_messages(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    store = InboxStore(path, clock)
    perception = decode_message(event(99, 2100, "旧正文", group=GROUP), str(ACCOUNT))
    assert perception is not None
    assert store.add(perception) is not None
    with closing(open_connection(path)) as connection, connection:
        connection.execute("ALTER TABLE inbox_messages DROP COLUMN attachments")
    conversation = ConversationKey.from_perception(perception)
    [entry] = InboxStore(path, clock).list_unread(conversation)
    assert entry.perception == perception
