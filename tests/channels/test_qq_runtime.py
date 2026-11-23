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

from aureus.common import AureusPaths, HeartbeatConfig, ManualClock, QQConfig
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.memory import LanguageKind, LanguageStore
from aureus.organs import KeyResolver, OrganBus, OrganRequest, OrganResponse
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery
from aureus.web import PasswordStore
from tests.test_language_runtime import LanguageOrgan

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
async def run_chat(
    paths: AureusPaths, *chat_args: str, request_log: Path | None = None
) -> AsyncIterator[asyncio.subprocess.Process]:
    arguments = ["--home", str(paths.home), "chat", *chat_args]
    command = ["-m", "aureus", *arguments, "--fake"]
    if request_log is not None:
        command = [
            "-c",
            "from tests.channels.test_qq_runtime import capture_language_requests; "
            "from aureus.cli.main import main; "
            "import sys; capture_language_requests(sys.argv.pop(1)); sys.exit(main())",
            str(request_log),
            *arguments,
        ]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *command,
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


def capture_language_requests(log_file: str) -> None:
    """子进程保留 CLI 和器官网关，器官总线只注册本地替身。"""
    path = Path(log_file)

    class RecordingOrgan(LanguageOrgan):
        async def process(self, request: OrganRequest) -> OrganResponse:
            with path.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps(
                        {
                            "organ": self.organ_id,
                            "task": request.context["task"],
                            "prompt": request.prompt,
                            "system_context": request.system_context,
                            "source_ids": request.context.get("source_ids", []),
                            "in_reply_to": request.context.get("in_reply_to", []),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            if request.context["task"] == "language_learning" and "月兔冲线" not in request.prompt:
                return OrganResponse(organ_id=self.organ_id, suggestion='{"proposals":[]}')
            return await super().process(request)

    def build_bus(
        _paths: AureusPaths,
        _heartbeat: HeartbeatConfig,
        resolver: KeyResolver,
        *,
        fake_organs: bool,
    ) -> ModelRuntime:
        assert not fake_organs  # CLI 的 --fake 会把 Planner 路径关掉。
        bus = OrganBus()
        for organ_id in ("planner", "replyer", "summarizer"):
            bus.register(RecordingOrgan(organ_id, []))
        return ModelRuntime(bus, resolver, fake_organs=True)

    runtime._build_organ_bus = build_bus


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
        runtime_config_bytes = paths.runtime_config_file.read_bytes()
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
            assert (await web.post(link_path, json=request, headers=headers)).status_code == 200
            revoked = await web.delete(link_path, headers=headers)
            assert revoked.status_code == 200 and revoked.json() == {"ok": True}
            assert (await web.post(f"{link_path}/confirm", headers=headers)).status_code == 409
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
    assert paths.runtime_config_file.read_bytes() == runtime_config_bytes
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
async def test_qq_chat_entry_learns_once_and_recalls_across_group_and_private(
    tmp_path: Path,
) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_bytes(IDENTITY)
    fake = FakeOneBot()
    fake.reply_with.clear()
    store = LanguageStore(paths.database_file)
    request_log = tmp_path / "organ-requests.jsonl"
    samples = [
        event(number, 2100, f"跑步第 {number} 场：GG，月兔冲线。", group=GROUP)
        for number in range(101, 105)
    ]
    async with asyncio.timeout(60), serve(fake.handle, "127.0.0.1", 0) as server:
        qq_port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            f"""[web]
port = {available_port()}
[heartbeat]
planning_enabled = true
split_reply_enabled = false
tick_interval_seconds = 1
[body]
sleep_enabled = false
[judge]
enabled = false
[language]
learning_enabled = true
[diary]
enabled = false
[qq]
enabled = true
account_id = "{ACCOUNT}"
endpoint = "ws://127.0.0.1:{qq_port}/onebot"
token_key = "{TOKEN_ENV}"
""",
            encoding="utf-8",
        )
        async with run_chat(paths, request_log=request_log):
            remote = await fake.connections.get()
            for sample in samples:
                await fake.send(remote, **sample)
                reply = await fake.messages.get()
                assert reply["group_id"] == str(GROUP)
                assert reply["message"][0]["data"]["id"] == str(sample["message_id"])
            async with asyncio.timeout(10):
                while sum(entry.count for entry in store.entries(LanguageKind.JARGON)) < 4:
                    await asyncio.sleep(0.05)

        async with run_chat(paths, request_log=request_log):
            remote = await fake.connections.get()
            await fake.send(remote, **samples[0])
            assert (await fake.messages.get())["message"][0]["data"]["id"] == "101"
            await fake.send(remote, **event(105, 2200, "gg，来聊跑步。"))
            reply = await fake.messages.get()
            assert reply == {
                "message_type": "private",
                "message": [
                    {"type": "reply", "data": {"id": "105"}},
                    {"type": "text", "data": {"text": "收到。"}},
                ],
                "user_id": "2200",
            }
            async with asyncio.timeout(10):
                while not any(
                    "gg，来聊跑步。" in line and '"task": "language_learning"' in line
                    for line in request_log.read_text(encoding="utf-8").splitlines()
                ):
                    await asyncio.sleep(0.05)

    assert paths.identity_file.read_bytes() == IDENTITY
    timeline = TimelineDatabase(
        paths.database_file, ManualClock(datetime(2026, 11, 22, tzinfo=UTC))
    )
    timeline.open()
    try:
        messages = timeline.query(TimelineQuery(event_type="message", limit=100))
    finally:
        timeline.close()
    inbound = [message for message in messages if message.actor_type is ActorType.PERSON]
    assert [message.metadata["source_message_id"] for message in inbound] == [
        "9000:101",
        "9000:102",
        "9000:103",
        "9000:104",
        "9000:101",
        "9000:105",
    ]
    evidence_ids = tuple(message.id for message in inbound[:4])
    expression = store.entries(LanguageKind.EXPRESSION)
    assert [(entry.text, entry.count, entry.evidence_ids) for entry in expression] == [
        ("月兔冲线", 4, evidence_ids)
    ]
    jargon = store.entries(LanguageKind.JARGON)
    assert [(entry.text, entry.detail, entry.count, entry.evidence_ids) for entry in jargon] == [
        ("GG", "抵达终点", 4, evidence_ids)
    ]

    requests = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
    learned_ids = [
        source_id
        for request in requests
        if request["task"] == "language_learning"
        for source_id in request["source_ids"]
    ]
    assert learned_ids == [*evidence_ids, inbound[-1].id]
    private_requests = {
        request["organ"]: request
        for request in requests
        if request["task"] in ("plan", "reply") and request["in_reply_to"] == [inbound[-1].id]
    }
    assert "GG：抵达终点" in private_requests["planner"]["prompt"]
    assert "当「庆祝跑步成绩」时，可用「月兔冲线」" in private_requests["replyer"]["prompt"]
    assert "Preserve this personality." in private_requests["replyer"]["system_context"]


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
