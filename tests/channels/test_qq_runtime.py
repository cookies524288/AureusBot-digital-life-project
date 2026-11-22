"""用回环 OneBot 和假器官验证 QQ 的正常命令行入口。"""

import asyncio
import json
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

from aureus.common import AureusPaths, ManualClock, QQConfig
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery
from aureus.web import PasswordStore

ACCOUNT = 9000
GROUP = 8000
PASSWORD = "only a synthetic password"
TOKEN = "qq-synthetic-token"
TOKEN_ENV = "AUREUS_KEY_QQ_PHASE36_TEST"
IDENTITY = b"""
[facts]
name = "Aureus Test"
birthday = "2008-01-01"

[user]
name = "Test Owner"

[persona]
immutable = "Preserve this personality."
"""


def event(
    message_id: int, sender: int, text: str, *, group: int | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "post_type": "message",
        "self_id": ACCOUNT,
        "user_id": sender,
        "message_id": message_id,
        "message_type": "group" if group else "private",
        "time": 1_795_360_000,
        "message": text,
        **({"group_id": group} if group else {}),
        **extra,
    }


class FakeOneBot:
    def __init__(self) -> None:
        self.connections: asyncio.Queue[ServerConnection] = asyncio.Queue()
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.reply_with = ["failed", "timeout"]
        self.handshakes: list[tuple[str, str | None]] = []

    async def send(self, remote: ServerConnection, **payload: Any) -> None:
        await remote.send(json.dumps(payload))

    async def handle(self, remote: ServerConnection) -> None:
        assert remote.request is not None
        self.handshakes.append((remote.request.path, remote.request.headers.get("Authorization")))
        async for frame in remote:
            request = json.loads(frame)
            response = {"status": "ok", "retcode": 0, "echo": request["echo"]}
            if request["action"] == "get_login_info":
                await self.send(remote, **response, data={"user_id": ACCOUNT})
                self.connections.put_nowait(remote)
                continue
            assert request["action"] == "send_msg"
            self.messages.put_nowait(request["params"])
            behavior = self.reply_with.pop(0) if self.reply_with else "ok"
            if behavior == "failed":
                await self.send(remote, **{**response, "status": "failed", "retcode": 100})
            elif behavior == "ok":
                await self.send(remote, **response, data={"message_id": 6000})


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@asynccontextmanager
async def run_chat(paths: AureusPaths) -> AsyncIterator[asyncio.subprocess.Process]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aureus",
        "--home",
        str(paths.home),
        "chat",
        "--fake",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, TOKEN_ENV: TOKEN, "PYTHONUTF8": "1"},
    )
    try:
        yield process
    finally:
        assert process.stdin is not None
        if process.returncode is None:
            process.stdin.write(b"/quit\n")
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise
        assert process.returncode == 0, output.decode("utf-8", errors="replace")
        assert TOKEN.encode() not in output


async def login(client: httpx.AsyncClient) -> None:
    async with asyncio.timeout(15):
        while True:
            try:
                response = await client.post("/api/login", json={"password": PASSWORD})
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
                continue
            assert response.status_code == 200, response.text
            return


@pytest.mark.asyncio
async def test_qq_chat_entry_reconnect_routing_and_account_link(tmp_path: Path) -> None:
    fake = FakeOneBot()
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_bytes(IDENTITY)
    PasswordStore(paths.web_password_file).set_password(PASSWORD)
    web_port = available_port()
    url = f"http://127.0.0.1:{web_port}"
    headers = {"Origin": url}

    async with asyncio.timeout(60), serve(fake.handle, "127.0.0.1", 0) as server:
        qq_port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            f"""[web]
port = {web_port}
[heartbeat]
split_reply_enabled = false
[qq]
enabled = true
account_id = "{ACCOUNT}"
endpoint = "ws://127.0.0.1:{qq_port}/onebot"
token_key = "{TOKEN_ENV}"
reconnect_seconds = 0.05
action_timeout_seconds = 0.4
""",
            encoding="utf-8",
        )
        async with run_chat(paths), httpx.AsyncClient(base_url=url, trust_env=False) as web:
            remote = await fake.connections.get()
            await login(web)
            assert fake.handshakes == [(f"/onebot?access_token={TOKEN}", f"Bearer {TOKEN}")]
            group_message = event(101, 2100, "", group=GROUP)
            group_message["message"] = [
                {"type": "at", "data": {"qq": str(ACCOUNT)}},
                {"type": "reply", "data": {"id": -99}},
                {"type": "text", "data": {"text": "合成群聊甲"}},
            ]
            await fake.send(remote, **group_message)
            await fake.send(remote, **event(102, 2200, "合成群聊乙", group=GROUP))
            await fake.send(remote, **event(103, 2100, "合成私聊", group_id=GROUP))
            await fake.send(remote, **event(6000, ACCOUNT, "机器人回显", group=GROUP))
            await fake.send(
                remote, **{**event(6000, ACCOUNT, "机器人回显"), "post_type": "message_sent"}
            )
            replies = [await fake.messages.get() for _ in range(3)]
            by_id = {item["message"][0]["data"]["id"]: item for item in replies}
            assert set(by_id) == {"101", "102", "103"}
            assert by_id["101"]["group_id"] == by_id["102"]["group_id"] == str(GROUP)
            assert by_id["103"]["message_type"] == "private"
            assert by_id["103"]["user_id"] == "2100"
            assert "group_id" not in by_id["103"]
            assert all(item["message"][1]["type"] == "text" for item in replies)

            await remote.close()
            remote = await fake.connections.get()
            await fake.send(remote, **group_message)
            await fake.send(remote, **event(104, 2300, "重连新消息", group=GROUP))
            assert (await fake.messages.get())["message"][0]["data"]["id"] == "104"

            link_path = "/api/qq-link"
            request = {"account_id": "2400"}
            assert (await web.post(link_path, json=request)).status_code == 403
            issued = await web.post(link_path, json=request, headers=headers)
            assert issued.status_code == 200
            assert issued.headers["Cache-Control"] == "no-store"
            command = issued.json()["command"]
            confirm_path = f"{link_path}/confirm"
            assert (await web.post(confirm_path, headers=headers)).status_code == 409
            await fake.send(remote, **event(105, 2401, command))
            await fake.send(remote, **event(106, 2400, command, group=GROUP))
            await fake.send(remote, **event(107, 2400, command))
            async with httpx.AsyncClient(base_url=url, trust_env=False) as second:
                assert (await second.post(confirm_path, headers=headers)).status_code == 401
                await login(second)
                assert (await second.post(confirm_path, headers=headers)).status_code == 409
            async with asyncio.timeout(5):
                while (
                    confirmed := await web.post(confirm_path, headers=headers)
                ).status_code == 409:
                    await asyncio.sleep(0.02)
            assert confirmed.json() == request
            assert (await web.post(confirm_path, headers=headers)).status_code == 409
            assert (
                await web.post(link_path, json={"account_id": "2100"}, headers=headers)
            ).status_code == 409
            assert (await web.get("/api/history")).json() == []

        async with run_chat(paths), httpx.AsyncClient(base_url=url, trust_env=False) as web:
            remote = await fake.connections.get()
            await login(web)
            await fake.send(remote, **event(108, 2400, "重启后用户私聊"))
            assert (await fake.messages.get())["user_id"] == "2400"
            history = (await web.get("/api/history")).json()
            assert [
                (item["who"], item["content"]) for item in history if item["who"] == "user"
            ] == [("user", "重启后用户私聊")]

    assert paths.identity_file.read_bytes() == IDENTITY
    timeline = TimelineDatabase(
        paths.database_file, ManualClock(datetime(2026, 11, 22, tzinfo=UTC))
    )
    timeline.open()
    try:
        messages = timeline.query(TimelineQuery(event_type="message", limit=100))
    finally:
        timeline.close()
    inbound = [item for item in messages if item.actor_type in (ActorType.PERSON, ActorType.USER)]
    assert [item.metadata["message_id"] for item in inbound] == ["101", "102", "103", "104", "108"]
    assert [item.actor_id for item in inbound] == ["2100", "2200", "2100", "2300", "2400"]
    assert inbound[0].metadata["quoted_message_id"] == "-99"
    assert inbound[0].metadata["mentioned"] is True
    assert inbound[0].metadata["source_timestamp"] == 1_795_360_000
    assert inbound[-1].actor_type is ActorType.USER
    assert command not in repr(messages)


@pytest.mark.asyncio
async def test_qq_disabled_in_the_normal_entry(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_bytes(IDENTITY)
    fake = FakeOneBot()
    async with serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            f'[web]\nport = {available_port()}\n[qq]\nendpoint = "ws://127.0.0.1:{port}"\n',
            encoding="utf-8",
        )
        PasswordStore(paths.web_password_file).set_password(PASSWORD)
        from aureus.common import load_settings

        url = f"http://127.0.0.1:{load_settings(paths).runtime.web.port}"
        async with run_chat(paths), httpx.AsyncClient(base_url=url, trust_env=False) as web:
            await login(web)
            assert "qq_link" not in (await web.get("/api/status")).json()
        assert fake.handshakes == []
        assert QQConfig().enabled is False
