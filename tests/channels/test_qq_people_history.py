"""在隔离库验证先迁入后绑定，跟踪两轮真实 QQ 入口的器官请求。"""

import asyncio
import json
from hashlib import md5
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.server import serve

from aureus.cli.main import main
from aureus.common import AureusPaths, ManualClock
from aureus.core.runtime import open_core
from aureus.people import HistoricalKind, PeopleDirectory
from aureus.web import PasswordStore
from tests.channels.test_qq_runtime import (
    ACCOUNT,
    IDENTITY,
    PASSWORD,
    FakeOneBot,
    available_port,
    event,
    login,
    run_chat,
)
from tests.cli.test_maibot_migration import SOURCE_ARGS, create_source, run_command
from tests.cli.test_maibot_people import _publish
from tests.heartbeat.test_judging_flow import NOW


def stage_people(tmp_path: Path) -> Path:
    staging = tmp_path / "stage.sqlite"
    for role, schema in (
        (
            "main",
            """
            CREATE TABLE person_info (id INTEGER PRIMARY KEY, person_id, platform,
                                      user_id, user_nickname);
            INSERT INTO person_info VALUES (1,'alpha','qq','2100','同名'),
                                         (2,'beta','qq','2200','同名');
            CREATE TABLE mai_messages (id INTEGER PRIMARY KEY, message_id, timestamp,
                                       platform, user_id, user_nickname, session_id,
                                       processed_plain_text);
            INSERT INTO mai_messages VALUES
                (71,'71','2026-10-22','qq','2100','同名','group','信物藏在青铜匣'),
                (72,'72','2026-10-23','qq','2200','同名','group','信物藏在白玉瓶');
        """,
        ),
        (
            "people",
            """
            CREATE TABLE events (id INTEGER PRIMARY KEY, person_id, kind, ts, detail);
            INSERT INTO events VALUES (1,'alpha','经历',100,'一起修过铜钟'),
                                      (2,'beta','经历',101,'一起绘过星图');
        """,
        ),
        (
            "memory",
            """
            CREATE TABLE person_profile_overrides (person_id TEXT PRIMARY KEY, override_text);
            INSERT INTO person_profile_overrides VALUES ('alpha','善于辨音'),
                                      ('beta','喜欢观星'), ('unknown','孤立画像');
        """,
        ),
    ):
        source, package = tmp_path / f"{role}.sqlite", tmp_path / f"{role}.zip"
        # 使用上游人物主键格式，让两个同昵称账号对应不同的记忆来源键。
        for alias, account in (("alpha", "2100"), ("beta", "2200")):
            key = md5(f"qq_{account}".encode(), usedforsecurity=False).hexdigest()
            schema = schema.replace(f"'{alias}'", f"'{key}'")
        create_source(source, schema)
        assert run_command("export", role, source, package) == 0
        assert run_command("import", role, package, staging) == 0
    return staging


@pytest.mark.asyncio
async def test_verified_qq_recalls_imported_person_after_restart(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_bytes(IDENTITY)
    PasswordStore(paths.web_password_file).set_password(PASSWORD)
    with open_core(paths, fake_organs=True, clock=ManualClock(NOW), to_console=False):
        pass
    staging = stage_people(tmp_path)
    assert _publish(staging, paths.home) == 0
    assert (
        main(
            [
                "migrate-maibot",
                "publish-memory",
                str(staging),
                "--instance",
                SOURCE_ARGS[1],
                "--source-timezone",
                "UTC",
                "--main-media-root",
                str(tmp_path),
                "--memory-media-root",
                str(tmp_path),
                "--target-home",
                str(paths.home),
            ]
        )
        == 0
    )
    people = PeopleDirectory(paths.database_file, ManualClock(NOW))
    people.open()
    try:
        user = people.find("web", "local")
        contact = people.find("qq", "2100")
        other = people.find("qq", "2200")
        assert user and contact and other and len({user.id, contact.id, other.id}) == 3
    finally:
        people.close()
    fake = FakeOneBot()
    fake.reply_with.clear()
    url = f"http://127.0.0.1:{available_port()}"
    headers = {"Origin": url}
    log = tmp_path / "requests.jsonl"
    async with asyncio.timeout(60), serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        paths.runtime_config_file.write_text(
            f"[web]\nport = {url.rsplit(':', 1)[1]}\n"
            "[heartbeat]\nplanning_enabled = true\nsplit_reply_enabled = false\n"
            "tick_interval_seconds = 1\n[body]\nsleep_enabled = false\n"
            "[judge]\nenabled = false\n[diary]\nenabled = false\n"
            "[memory]\nenabled = true\nrecall_enabled = true\nheuristic_recall_enabled = true\n"
            f'[qq]\nenabled = true\naccount_id = "{ACCOUNT}"\n'
            f'endpoint = "ws://127.0.0.1:{port}/onebot"\n',
            encoding="utf-8",
        )
        async with (
            run_chat(paths, request_log=log),
            httpx.AsyncClient(base_url=url, trust_env=False) as web,
        ):
            remote = await fake.connections.get()
            await login(web)
            issued = await web.post("/api/qq-link", json={"account_id": "2100"}, headers=headers)
            assert issued.status_code == 200, issued.text
            command = issued.json()["command"]
            await fake.send(remote, **event(10, 2100, command))
            async with asyncio.timeout(5):
                while (
                    confirmed := await web.post("/api/qq-link/confirm", headers=headers)
                ).status_code == 409:
                    await asyncio.sleep(0.02)
            assert confirmed.json() == {"account_id": "2100"}
            await fake.send(remote, **event(11, 2100, "我说过 信物？"))
            assert (await fake.messages.get())["message"][0]["data"]["id"] == "11"
        assert _publish(staging, paths.home) == 0
        async with run_chat(paths, request_log=log, browse_inbox=True):
            remote = await fake.connections.get()
            for number, sender in [(13, 2200), (12, 2100)]:
                message = event(number, sender, "我说过 信物？")
                message["sender"] = {"nickname": "同名"}
                await fake.send(remote, **message)
            for number, sender in [(12, 2100), (13, 2200)]:
                reply = await fake.messages.get()
                assert reply["user_id"] == str(sender)
                assert reply["message"][0]["data"]["id"] == str(number)
    requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    turns = [item for item in requests if item["task"] in ("plan", "reply")]
    assert [item["task"] for item in turns] == ["plan", "reply"] * 3
    for turn, (plan, reply) in enumerate(zip(turns[::2], turns[1::2], strict=True)):
        assert plan["in_reply_to"] == reply["in_reply_to"]
        prompt = reply["prompt"]
        assert ("善于辨音" in prompt) == (turn < 2)
        assert ("一起修过铜钟" in prompt) == (turn < 2)
        assert ("喜欢观星" in prompt) == (turn == 2)
        assert ("一起绘过星图" in prompt) == (turn == 2)
        assert "mai_messages/" in prompt
        assert ("信物藏在青铜匣" in prompt) == (turn < 2)
        assert ("信物藏在白玉瓶" in prompt) == (turn == 2)
        assert "孤立画像" not in prompt and command not in prompt
    people.open()
    try:
        assert people.find("qq", "2100") == user and people.find("qq", "2200") == other
        assert {entry.record.subject_key for entry in people.history_for(user.id)} == {
            md5(b"qq_2100", usedforsecurity=False).hexdigest()
        }
        unresolved = people.historical_record(
            SOURCE_ARGS[1], HistoricalKind.PROFILE_OVERRIDE, '["unknown"]'
        )
        assert unresolved and unresolved.pending_reason == "unresolved_person_reference"
        assert unresolved.person_id is None and unresolved.record.summary == "孤立画像"
    finally:
        people.close()
    assert paths.identity_file.read_bytes() == IDENTITY
