"""D93：部署范围由两个启动入口共用，每次按运行库的本人账号核对私聊。"""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve

from aureus.channels.onebot_transport import OneBotError
from aureus.common import AureusPaths, ManualClock
from aureus.heartbeat import MessageKind, MessageRoute, OutgoingMessage
from aureus.people import PeopleDirectory
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery
from tests.channels.test_qq_behavior_replay import START, open_replay
from tests.channels.test_qq_runtime import (
    ACCOUNT,
    GROUP,
    IDENTITY,
    FakeOneBot,
    available_port,
    event,
    run_chat,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("test_group", [None, str(GROUP), str(GROUP + 1)])
async def test_scope_intersection_and_current_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_group: str | None
) -> None:
    async with open_replay(
        tmp_path,
        monkeypatch,
        ManualClock(START),
        allowed_group_id=str(GROUP),
        qq_test_group=test_group,
    ) as replay:
        channel = replay.core.qq
        assert channel is not None
        # 假端发出登录回执后，等客户端完成账号校验才测试主动投递。
        while channel._transport.socket is None:
            await asyncio.sleep(0)

        def send(route: MessageRoute | None) -> None:
            channel.show(
                OutgoingMessage("qq", "2100", "主动发送", MessageKind.REPLY, "豆", route=route)
            )

        account = str(ACCOUNT)
        private = MessageRoute(account, "private", "2100", "91")
        group = MessageRoute(account, "group", str(GROUP), "92")
        for route in (None, private, MessageRoute(account, "group", str(GROUP + 1), "93")):
            with pytest.raises(OneBotError):
                send(route)
        people = replay.core.people
        owner = people.find("web", "local")
        assert owner and owner.is_user
        people.bind_verified_account(owner, "qq", "2100", None)
        for route in (
            MessageRoute(account, "private", "2200", "94"),
            MessageRoute(account, "private", "2100", "95", str(GROUP)),
            MessageRoute("9999", "private", "2100", "96"),
        ):
            with pytest.raises(OneBotError):
                send(route)
        if test_group is None:
            send(private)
            assert (await replay.fake.messages.get())["user_id"] == "2100"
        else:
            with pytest.raises(OneBotError):
                send(private)
        if test_group in (None, str(GROUP)):
            send(group)
            assert (await replay.fake.messages.get())["group_id"] == str(GROUP)
        else:
            with pytest.raises(OneBotError):
                send(group)
        assert replay.fake.messages.empty()


@pytest.mark.asyncio
async def test_deployment_scope_cli_then_windows_launcher(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_bytes(IDENTITY)
    clock = ManualClock(START)
    people = PeopleDirectory(paths.database_file, clock)
    people.open()
    try:
        owner = people.ensure_user("Test Owner", [("web", "local")])
        people.bind_verified_account(owner, "qq", "2100", None)
        people.meet("qq", "2200")
    finally:
        people.close()
    fake = FakeOneBot()
    fake.reply_with.clear()

    async def exchange(base: int) -> ServerConnection:
        remote = await fake.connections.get()
        for payload in (
            event(base, 2100, "其他群", group=GROUP + 1),
            event(base + 1, 2200, "联系人私聊"),
            event(base + 2, 2100, "临时私聊", sub_type="group", group_id=GROUP),
            event(base + 3, 2100, "缺群地址", sub_type="group"),
            event(base + 4, 2100, "临时来源", temp_source=0),
        ):
            await fake.send(remote, **payload)
        for offset, group in [(5, None), (6, GROUP)]:
            await fake.send(remote, **event(base + offset, 2100, "允许的消息", group=group))
            reply = await fake.messages.get()
            if group is None:
                assert reply["message"][0]["data"]["id"] == str(base + offset)
            else:
                assert [part["type"] for part in reply["message"]] == ["text"]
            assert reply.get("group_id") == (str(group) if group else None)
            assert reply.get("user_id") == (None if group else "2100")
        return remote

    async with asyncio.timeout(45), serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            f"[web]\nport = {available_port()}\n[heartbeat]\nsplit_reply_enabled = false\n"
            f'[qq]\nenabled = true\naccount_id = "{ACCOUNT}"\nallowed_group_id = "{GROUP}"\n'
            f'endpoint = "ws://127.0.0.1:{port}/onebot"\nreconnect_seconds = 0.1\n',
            encoding="utf-8",
        )
        async with run_chat(paths):
            first = await exchange(100)
            await first.close()
            await exchange(200)
        # 新进程走托盘使用的 Python 入口；关闭标准输入让托盘退出，不打开浏览器。
        if sys.platform == "win32":
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "aureus.cli.windows_launcher",
                "--fake",
                "--tray",
                "--restarted",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "AUREUS_HOME": str(paths.home), "PYTHONUTF8": "1"},
            )
            try:
                await exchange(300)
            finally:
                assert process.stdin is not None
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
            assert process.returncode == 0
    timeline = TimelineDatabase(paths.database_file, clock)
    timeline.open()
    try:
        records = timeline.query(TimelineQuery(event_type="message", limit=100))
        inbound = [r for r in records if r.actor_type in (ActorType.USER, ActorType.PERSON)]
        expected = ["105", "106", "205", "206"] + (
            ["305", "306"] if sys.platform == "win32" else []
        )
        assert [r.metadata["message_id"] for r in inbound] == expected
    finally:
        timeline.close()
