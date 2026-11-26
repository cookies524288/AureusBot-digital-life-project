"""用回环 OneBot 和假器官验证 QQ 的正常命令行入口。"""

import asyncio
import json
import os
import socket
import sys
from base64 import b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

from aureus.channels.onebot import decode_message
from aureus.common import AureusPaths, HeartbeatConfig, ManualClock, QQConfig
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import main_loop
from aureus.heartbeat.inbox_attention import InboxAttention
from aureus.heartbeat.inbox_store import InboxConversation
from aureus.heartbeat.reply_outbox import EmojiReplyPart
from aureus.memory import LanguageKind, LanguageStore
from aureus.organs import FakePlanner, KeyResolver, OrganBus, OrganRequest, OrganResponse
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery
from aureus.web import PasswordStore
from tests.prompt_assertions import dialogue_text
from tests.test_language_runtime import LanguageOrgan, learning_samples

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
    message_id: int, sender_id: int, text: str, *, group: int | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "post_type": "message",
        "self_id": ACCOUNT,
        "user_id": sender_id,
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
        self.receipt_ids: list[int] = []
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
            if request["action"] == "get_group_info":
                await self.send(remote, **response, data={"group_name": "合成群"})
                continue
            assert request["action"] == "send_msg"
            self.messages.put_nowait(request["params"])
            behavior = self.reply_with.pop(0) if self.reply_with else "ok"
            if behavior == "failed":
                await self.send(remote, **{**response, "status": "failed", "retcode": 100})
            elif behavior == "ok":
                receipt_id = self.receipt_ids.pop(0) if self.receipt_ids else 6000
                await self.send(remote, **response, data={"message_id": receipt_id})


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@asynccontextmanager
async def run_chat(
    paths: AureusPaths, *chat_args: str, request_log: Path | None = None, browse_inbox: bool = False
) -> AsyncIterator[asyncio.subprocess.Process]:
    arguments = ["--home", str(paths.home), "chat", *chat_args]
    setup = (
        "from tests.channels.test_qq_runtime import capture_language_requests, drive_inbox_clock; "
        "from aureus.cli.main import main; import sys; drive_inbox_clock(); "
    )
    fixture_args: list[str] = []
    if request_log is not None:
        setup += "capture_language_requests(sys.argv.pop(1), sys.argv.pop(1) == '1'); "
        fixture_args = [str(request_log), "1" if browse_inbox else "0"]
    else:
        arguments.append("--fake")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        setup + "sys.exit(main())",
        *fixture_args,
        *arguments,
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


def drive_inbox_clock() -> None:
    """推进收件箱的手动时钟，保留正常 CLI 和其他子系统的时序。"""
    clock = ManualClock(datetime(2026, 11, 23, 12, tzinfo=UTC))

    class TimedInboxAttention(InboxAttention):
        def browse(self, now: datetime, *, can_browse: bool) -> list[InboxConversation]:
            clock.advance(timedelta(minutes=5))
            return super().browse(clock.now(), can_browse=can_browse)

    main_loop.InboxAttention = TimedInboxAttention


def capture_language_requests(log_file: str, browse_inbox: bool = False) -> None:
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
                            "prompt": dialogue_text(request),
                            "system_context": request.system_context,
                            "samples": learning_samples(request)
                            if request.context["task"] in ("learn_style", "learn_jargon")
                            else [],
                            "in_reply_to": request.context.get("in_reply_to", []),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            if request.context["task"] == "learn_jargon" and "月兔冲线" not in dialogue_text(
                request
            ):
                return OrganResponse(organ_id=self.organ_id, suggestion="[]")
            if request.context["task"] == "inbox_select":
                return await FakePlanner(self.organ_id).process(request)
            response = await super().process(request)
            if browse_inbox and request.context["task"] == "plan":
                response.tool_calls[0].arguments["read_inbox"] = True
            return response

    def build_bus(
        _paths: AureusPaths,
        _heartbeat: HeartbeatConfig,
        resolver: KeyResolver,
        *,
        fake_organs: bool,
        **_kwargs: object,
    ) -> ModelRuntime:
        assert not fake_organs
        bus = OrganBus()
        for organ_id in ("planner", "replyer", "summarizer", "utils"):
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
tick_interval_seconds = 1
[body]
sleep_enabled = false
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
            first = await fake.messages.get()
            await fake.send(remote, **event(102, 2200, "合成群聊乙", group=GROUP))
            second_reply = await fake.messages.get()
            await fake.send(remote, **event(103, 2100, "合成私聊", group_id=GROUP))
            await fake.send(remote, **event(6000, ACCOUNT, "机器人回显", group=GROUP))
            await fake.send(
                remote, **{**event(6000, ACCOUNT, "机器人回显"), "post_type": "message_sent"}
            )
            private = await fake.messages.get()
            assert [part["type"] for part in first["message"]] == ["text"]
            assert [part["type"] for part in second_reply["message"]] == ["text"]
            assert first["group_id"] == second_reply["group_id"] == str(GROUP)
            assert private["message_type"] == "private"
            assert private["user_id"] == "2100" and "group_id" not in private
            assert [part["type"] for part in private["message"]] == ["text"]

            await remote.close()
            remote = await fake.connections.get()
            await fake.send(remote, **group_message)
            await fake.send(remote, **event(104, 2300, "重连新消息", group=GROUP))
            assert [part["type"] for part in (await fake.messages.get())["message"]] == ["text"]

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
        for number in range(101, 112)
    ]
    async with asyncio.timeout(60), serve(fake.handle, "127.0.0.1", 0) as server:
        qq_port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            f"""[web]
port = {available_port()}
[heartbeat]
planning_enabled = true
message_debounce_mode = "off"
planner_message_steering = false
working_memory_capacity = 22
working_memory_retain = 2
split_reply_enabled = false
tick_interval_seconds = 1
[body]
sleep_enabled = false
[judge]
enabled = false
[language]
learning_enabled = true
expression_quality_enabled = false
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
                assert [part["type"] for part in reply["message"]] == ["text"]
            async with asyncio.timeout(10):
                while sum(entry.count for entry in store.entries(LanguageKind.JARGON)) < 10:
                    await asyncio.sleep(0.05)

        async with run_chat(paths, request_log=request_log):
            remote = await fake.connections.get()
            await fake.send(remote, **samples[0])
            await fake.send(remote, **event(112, 2200, "gg，来聊跑步。"))
            reply = await fake.messages.get()
            assert reply == {
                "message_type": "private",
                "message": [{"type": "text", "data": {"text": "收到。"}}],
                "user_id": "2200",
            }

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
        *(f"9000:{number}" for number in range(101, 113)),
    ]
    evidence_ids = tuple(message.id for message in inbound[:10])
    expression = store.entries(LanguageKind.EXPRESSION)
    assert [(entry.text, entry.count, entry.evidence_ids) for entry in expression] == [
        ("月兔冲线", 10, evidence_ids)
    ]
    jargon = store.entries(LanguageKind.JARGON)
    assert [(entry.text, entry.detail, entry.count, entry.evidence_ids) for entry in jargon] == [
        ("GG", "抵达终点", 10, evidence_ids)
    ]

    requests = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
    learned_texts = [
        sample["text"]
        for request in requests
        if request["task"] == "learn_jargon"
        for sample in request["samples"]
    ]
    assert learned_texts in (
        [row.content for row in inbound[:10]],
        [row.content for row in inbound],
    )
    private_requests = {
        request["organ"]: request
        for request in requests
        if request["task"] in ("plan", "reply") and request["in_reply_to"] == [inbound[-1].id]
    }
    assert "GG：抵达终点" in private_requests["planner"]["prompt"]
    assert "当「庆祝跑步成绩」时，可用「月兔冲线」" in private_requests["replyer"]["prompt"]
    assert "Preserve this personality." in private_requests["replyer"]["system_context"]


@pytest.mark.asyncio
async def test_qq_chat_entry_debounces_two_rapid_messages_into_one_reply(
    tmp_path: Path,
) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_bytes(IDENTITY)
    fake = FakeOneBot()
    fake.reply_with.clear()
    request_log = tmp_path / "organ-requests.jsonl"
    async with asyncio.timeout(60), serve(fake.handle, "127.0.0.1", 0) as server:
        qq_port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            f"""[web]
port = {available_port()}
[heartbeat]
message_debounce_mode = "steady"
split_reply_enabled = false
tick_interval_seconds = 1
[body]
sleep_enabled = false
[judge]
enabled = false
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
            await fake.send(remote, **event(201, 2100, "连发甲", group=GROUP))
            await fake.send(remote, **event(202, 2100, "连发乙", group=GROUP))
            reply = await asyncio.wait_for(fake.messages.get(), timeout=20)
            assert reply["group_id"] == str(GROUP)
            assert [part["type"] for part in reply["message"]] == ["text"]
            await asyncio.sleep(6)
            assert fake.messages.empty()

    requests = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
    plans = [request for request in requests if request["task"] == "plan"]
    replies = [request for request in requests if request["task"] == "reply"]
    assert len(plans) == 1
    assert all(text in plans[0]["prompt"] for text in ("连发甲", "连发乙"))
    assert len(plans[0]["in_reply_to"]) == 2
    assert len(replies) == 1
    assert replies[0]["in_reply_to"] == plans[0]["in_reply_to"]

    timeline = TimelineDatabase(
        paths.database_file, ManualClock(datetime(2026, 11, 22, tzinfo=UTC))
    )
    timeline.open()
    try:
        messages = timeline.query(TimelineQuery(event_type="message", limit=100))
    finally:
        timeline.close()
    inbound = [message for message in messages if message.actor_type is ActorType.PERSON]
    assert [message.content for message in inbound] == ["连发甲", "连发乙"]


@pytest.mark.asyncio
async def test_qq_emoji_order_scope_and_successful_use(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_bytes(IDENTITY)
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    fake = FakeOneBot()
    fake.reply_with[:] = ["failed", "ok", "ok", "ok"]
    image = b"GIF89a synthetic image"

    async with asyncio.timeout(10), serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            f'[qq]\nenabled = true\naccount_id = "{ACCOUNT}"\n'
            f'endpoint = "ws://127.0.0.1:{port}"\nallowed_group_id = "{GROUP}"\n'
            "[heartbeat]\ntyping_delay_enabled = false\n",
            encoding="utf-8",
        )
        with runtime.open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
            channel, store = core.qq, core.emoji_store
            assert channel is not None and store is not None
            digest = store.collect(image).digest
            assert digest is not None
            store.set_status(digest, is_registered=True, is_banned=False)
            record = core.timeline.append("message", "测试回复", ActorType.ME)
            async with channel:
                await fake.connections.get()
                while channel._transport.socket is None:
                    await asyncio.sleep(0)
                target = decode_message(event(1, 2100, "表情", group=GROUP), str(ACCOUNT))
                other = decode_message(event(2, 2100, "表情", group=GROUP + 1), str(ACCOUNT))
                assert target is not None and other is not None
                delivered = core.heartbeat._deliver_reply
                assert not delivered(other, record, ("拒绝",), emoji=EmojiReplyPart(digest))

                assert delivered(
                    target, record, ("前置",), emoji=EmojiReplyPart(digest, True), quote_source=True
                )
                assert delivered(
                    target, record, ("后置",), emoji=EmojiReplyPart(digest), quote_source=True
                )
                messages = [await fake.messages.get() for _ in range(4)]
                assert {message["group_id"] for message in messages} == {str(GROUP)}
                parts = [message["message"] for message in messages]
                quote = {"type": "reply", "data": {"id": "1"}}
                expected = {
                    "type": "image",
                    "data": {"file": "base64://" + b64encode(image).decode(), "sub_type": 1},
                }
                assert parts == [
                    [quote, expected],
                    [{"type": "text", "data": {"text": "前置"}}],
                    [quote, {"type": "text", "data": {"text": "后置"}}],
                    [expected],
                ]
                await asyncio.gather(*channel._sending)
                assert store.browse("", 0, 10)[0].use_count == 1

                assert delivered(target, record, ("暂停前",), emoji=EmojiReplyPart(digest))
                core.heartbeat.set_maintenance_paused(True)
                assert (await fake.messages.get())["message"][0]["data"]["text"] == "暂停前"
                await asyncio.gather(*channel._sending)
                assert fake.messages.empty() and store.browse("", 0, 10)[0].use_count == 1

                core.heartbeat.set_maintenance_paused(False)
                assert delivered(target, record, ("急停前",), emoji=EmojiReplyPart(digest))
                assert core.heartbeat.halt("合成急停")
                assert (await fake.messages.get())["message"][0]["data"]["text"] == "急停前"
                await asyncio.gather(*channel._sending)
                assert fake.messages.empty() and store.browse("", 0, 10)[0].use_count == 1


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
