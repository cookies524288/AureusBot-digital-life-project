"""桌面操作从临时登录、收件箱打通到回环上的 SnowLuma 协议替身。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

from aureus.channels.qq import QQChannel
from aureus.common import ManualClock, QQConfig
from aureus.heartbeat.inbox_store import InboxStore
from tests.channels.test_qq_inbox import Receiver
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, event
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import GOOD, NOW

PEER = 2100
OTHER_GROUP = GROUP + 1
BASE = "/api/inbox/operations"
SCOPE = {"platform": "qq", "account_id": str(ACCOUNT), "recipient_id": str(GROUP), "kind": "group"}
ORIGIN = {"Origin": "http://testserver"}


@pytest.mark.asyncio
async def test_desktop_checks_confirmation_scope_and_echo_before_receipt(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    calls: list[dict[str, Any]] = []
    allowed = True
    revoke_on_member = False
    private = {**SCOPE, "recipient_id": str(PEER), "kind": "private"}
    ready = asyncio.Event()

    async def handle(socket: ServerConnection) -> None:
        nonlocal allowed
        async for raw in socket:
            call = json.loads(raw)
            calls.append(call)
            reply: Any = None
            status, retcode = "ok", 0
            match call["action"]:
                case "get_login_info":
                    reply = {"user_id": ACCOUNT}
                case "get_group_info":
                    reply = {"group_name": "测试群"}
                case "get_msg":
                    reply = {
                        "message_id": -10, "message_type": "group",
                        "group_id": GROUP, "user_id": PEER,
                    }
                case "get_group_member_info":
                    reply = {"group_id": GROUP, "user_id": PEER, "nickname": "合成成员"}
                    if revoke_on_member:
                        allowed = False
                case "send_msg":
                    if call["params"]["message_type"] == "group":
                        reply = {"message_id": -55}
                case "send_poke" if "group_id" in call["params"]:
                    status, retcode = "failed", 1404
                case "upload_group_file":
                    reply = {"file_id": "/fixture-group-file"}
                case "upload_private_file":
                    reply = {"file_id": "fixture-private-file"}
            await socket.send(json.dumps({
                "echo": call["echo"], "status": status, "retcode": retcode, "data": reply,
            }))
            if call["action"] == "get_login_info":
                records = (event(-10, PEER, "群原件", group=GROUP), event(11, PEER, "私聊原件"))
                for record in records:
                    await socket.send(json.dumps(record))

    def notice(_message: Any) -> None:
        if len(store.visible_conversation_keys()) == 2:
            ready.set()

    async with asyncio.timeout(10), serve(handle, "127.0.0.1", 0) as server:
        address = server.sockets[0].getsockname()
        config = QQConfig(
            enabled=True, account_id=str(ACCOUNT), allowed_group_id=str(GROUP),
            endpoint=f"ws://127.0.0.1:{address[1]}",
        )
        async with QQChannel(
            config, None, Receiver(), lambda account: account == str(PEER),
            inbox_store=store, on_inbox_message=notice,
        ) as channel:
            await ready.wait()
            rig = ConfigRig(tmp_path, inbox_store=store, qq_desktop=channel.desktop,
                            allows_inbox_conversation=channel.allows_inbox_conversation,
                            allows_qq_delivery=lambda: allowed)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=rig.app), base_url="http://testserver"
                ) as client:
                    async def submit(
                        kind: str, body: dict[str, Any], *, scope: dict[str, str] = SCOPE,
                        headers: dict[str, str] = ORIGIN,
                    ) -> httpx.Response:
                        response = await client.post(
                            f"{BASE}/{kind}", params=scope, headers=headers, json=body
                        )
                        rig.clock.advance(timedelta(seconds=1))
                        return response

                    text = {"confirmed": True, "text": "合成桌面消息"}
                    assert (await submit("text", text)).status_code == 401
                    assert (await client.post("/api/login", json={"password": GOOD})).is_success
                    foreign = {"Origin": "http://foreign.invalid"}
                    assert (await submit("text", text, headers=foreign)).status_code == 403
                    assert (await submit("text", {**text, "confirmed": False})).status_code == 422
                    assert (await submit("text", {**text, "action": "send_msg"})).status_code == 422
                    other = {**SCOPE, "recipient_id": str(OTHER_GROUP)}
                    temporary = {**private, "temporary_group_id": str(GROUP)}
                    assert (await submit("text", text, scope=other)).status_code == 403
                    assert (await submit("text", text, scope=temporary)).status_code == 403
                    page = (await client.get("/api/inbox/messages", params=SCOPE)).json()
                    anchor = page["messages"][0]["id"]
                    quoted = {**text, "quote": anchor}
                    assert (await submit("text", quoted, scope=private)).status_code == 404
                    assert (await submit("text", {**quoted, "mention": anchor})).json() == {
                        "message_id": "-55",
                    }
                    sent = next(call["params"] for call in calls if call["action"] == "send_msg")
                    assert sent == {"group_id": str(GROUP), "message_type": "group", "message": [
                        {"type": "reply", "data": {"id": "-10"}},
                        {"type": "at", "data": {"qq": str(PEER)}},
                        {"type": "text", "data": {"text": text["text"]}},
                    ]}
                    page = (await client.get("/api/inbox/messages", params=SCOPE)).json()
                    outbound = page["messages"][0]
                    assert outbound["direction"] == "outgoing"
                    assert outbound["platform_message_id"] == "-55"
                    assert [part["kind"] for part in outbound["components"]] == [
                        "reply", "at", "text",
                    ]
                    poke = {"confirmed": True}
                    assert (await submit("poke", {**poke, "member": anchor})).status_code == 501
                    assert (await submit("poke", poke, scope=private)).json() == {"accepted": True}
                    assert (await submit("text", text, scope=private)).status_code == 502
                    async def upload(
                        scope: dict[str, str], name: str, data: bytes, mime: str = "text/plain",
                    ) -> httpx.Response:
                        response = await client.post(
                            f"{BASE}/file", params={**scope, "name": name, "confirmed": "yes"},
                            headers={**ORIGIN, "Content-Type": mime}, content=data,
                        )
                        rig.clock.advance(timedelta(seconds=1))
                        return response

                    assert (await upload(SCOPE, "C:/private.txt", b"x")).status_code == 422
                    assert (await upload(SCOPE, "ok.png", b"x", "image/png")).status_code == 415
                    oversized = b"x" * (700 * 1024 + 1)
                    assert (await upload(SCOPE, "ok.txt", oversized)).status_code == 413
                    assert (await upload(SCOPE, "合成.txt", b"data")).json() == {
                        "file_id": "/fixture-group-file",
                    }
                    assert (await upload(private, "test.txt", b"data")).json() == {
                        "file_id": "fixture-private-file",
                    }
                    uploads = [call["params"] for call in calls if call["action"].startswith("upload_")]
                    assert uploads == [
                        {"group_id": str(GROUP), "name": "合成.txt", "file": "base64://ZGF0YQ=="},
                        {"user_id": str(PEER), "name": "test.txt", "file": "base64://ZGF0YQ=="},
                    ]
                    revoke_on_member = True
                    assert (await submit("text", {**text, "mention": anchor})).status_code == 403
                    assert [call["action"] for call in calls].count("send_msg") == 2
                    page = (await client.get("/api/inbox/messages", params=private)).json()
                    assert len(page["messages"]) == 1
            finally:
                rig.close()
