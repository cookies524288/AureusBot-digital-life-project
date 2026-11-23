"""用回环 OneBot、假时钟和假器官核对单次进程的测试群边界。"""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from aureus.channels.onebot import decode_message
from aureus.channels.onebot_transport import OneBotError
from aureus.cli.main import build_parser
from aureus.common import AureusPaths, ManualClock
from aureus.heartbeat import (
    EVENT_INITIATIVE_CONSIDERED,
    MessageKind,
    MessageRoute,
    OutgoingMessage,
    Perception,
)
from aureus.timeline import ActorType, TimelineDatabase
from tests.channels.test_qq_behavior_replay import START, open_replay
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, IDENTITY, FakeOneBot, event, run_chat


@pytest.mark.asyncio
async def test_only_test_group_reaches_heartbeat_and_wire_after_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, qq_test_group=str(GROUP)) as replay:
        channel = replay.core.qq
        assert channel is not None
        replay.planner.plan = {"reply": True, "intent": "回答点名"}
        commands: list[str] = []
        consume = channel._consume_command
        assert consume is not None

        def observe_command(perception: Perception) -> bool:
            assert perception.source_message_id is not None
            commands.append(perception.source_message_id)
            return consume(perception)

        monkeypatch.setattr(channel, "_consume_command", observe_command)
        for start in (100, 200):
            if start == 200:
                await replay.remote.close()
                replay.remote = await replay.fake.connections.get()
            for payload in (
                event(start, 2100, "其他群", group=GROUP + 1),
                event(start + 1, 2100, "普通私聊"),
                event(start + 2, 2100, "临时私聊", sub_type="group", group_id=GROUP),
            ):
                await replay.fake.send(replay.remote, **payload)
            for route in (
                MessageRoute(str(ACCOUNT), "group", str(GROUP + 1), "9"),
                MessageRoute(str(ACCOUNT), "private", str(GROUP), "9"),
                MessageRoute(str(ACCOUNT), "private", "2100", "9", str(GROUP)),
            ):
                with pytest.raises(OneBotError):
                    channel.show(
                        OutgoingMessage("qq", "2100", "回投", MessageKind.REPLY, "豆", route=route)
                    )
            addressed = event(start + 3, 2100, "", group=GROUP)
            addressed["message"] = [
                {"type": "at", "data": {"qq": str(ACCOUNT)}},
                {"type": "text", "data": {"text": "合成点名"}},
            ]
            await replay.receive(addressed)
            await replay.step()
            await replay.assert_sent(start + 3)
        assert commands == [f"{ACCOUNT}:103", f"{ACCOUNT}:203"]
        assert replay.fake.messages.empty()
        inbound = [row for row in replay.records() if row.actor_type is ActorType.PERSON]
        assert [row.metadata["message_id"] for row in inbound] == ["103", "203"]
        assert all(row.metadata["mentioned"] for row in inbound)
        for organ in (replay.planner, replay.replyer):
            assert [req.context["in_reply_to"] for _, req, _ in organ.calls] == [
                [row.id] for row in inbound
            ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("test_only", "persistent", "bound", "sent"),
    [
        (False, False, False, True),
        (True, False, False, False),
        (False, True, False, False),
        (False, True, True, True),
        (True, True, True, False),
    ],
)
async def test_old_conversation_drives_initiative_but_private_send_obeys_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_only: bool,
    persistent: bool,
    bound: bool,
    sent: bool,
) -> None:
    clock = ManualClock(START)
    timeline = TimelineDatabase(AureusPaths(tmp_path).database_file, clock)
    timeline.open()
    try:
        timeline.append("message", "之前的对话", ActorType.ME, metadata={"to_user": True})
    finally:
        timeline.close()
    clock.advance(timedelta(hours=20))
    async with open_replay(
        tmp_path,
        monkeypatch,
        clock,
        initiative=True,
        qq_test_group=str(GROUP) if test_only else None,
        allowed_group_id=str(GROUP) if persistent else None,
    ) as replay:
        # 醒来时从时间线恢复的是对话时刻。另注入已知私聊回址，单独挑战发送边界。
        target = decode_message(event(90, 2100, "旧私聊"), str(ACCOUNT))
        assert target is not None
        if bound:
            owner = replay.core.people.find("web", "local")
            assert owner and owner.is_user
            replay.core.people.bind_verified_account(owner, "qq", target.sender_id, None)
        monkeypatch.setattr(replay.core.heartbeat, "_last_user_contact", target)
        replay.planner.plan = {"reach_out": True, "intent": "问候", "reason": "好久没聊"}
        await replay.step()
        [decision] = replay.records(EVENT_INITIATIVE_CONSIDERED)
        assert decision.metadata["social_need"] > 0.7
        assert decision.metadata["sent"] is sent
        if not sent:
            assert decision.metadata["decided"] == "failed"
            assert replay.fake.messages.empty()
        else:
            assert (await replay.fake.messages.get())["user_id"] == "2100"


@pytest.mark.asyncio
async def test_cli_scope_lasts_one_process(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_bytes(IDENTITY)
    fake = FakeOneBot()
    fake.reply_with.clear()
    async with asyncio.timeout(30), serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            "[heartbeat]\nsplit_reply_enabled = false\n[web]\nenabled = false\n"
            f'[qq]\nenabled = true\naccount_id = "{ACCOUNT}"\n'
            f'endpoint = "ws://127.0.0.1:{port}/onebot"\n',
            encoding="utf-8",
        )
        config = paths.runtime_config_file.read_bytes()
        async with run_chat(paths, "--qq-test-group", str(GROUP)):
            remote = await fake.connections.get()
            await fake.send(remote, **event(21, 2100, "私聊"))
            await fake.send(remote, **event(22, 2100, "测试群", group=GROUP))
            reply = await fake.messages.get()
            assert reply["group_id"] == str(GROUP) and reply["message"][0]["data"]["id"] == "22"
        async with run_chat(paths):
            remote = await fake.connections.get()
            await fake.send(remote, **event(23, 2100, "重新启动后的私聊"))
            reply = await fake.messages.get()
            assert reply["user_id"] == "2100" and reply["message"][0]["data"]["id"] == "23"
            await fake.send(remote, **event(24, 2100, "其他群", group=GROUP + 1))
            reply = await fake.messages.get()
            assert reply["group_id"] == str(GROUP + 1) and reply["message"][0]["data"]["id"] == "24"
    assert paths.runtime_config_file.read_bytes() == config


@pytest.mark.parametrize("invalid", ["", "0", "08000", "all", "8000,8001"])
def test_invalid_test_group_is_rejected_by_cli(invalid: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["chat", "--qq-test-group", invalid])
    assert error.value.code == 2
