"""网页服务组装的测试：开关、接到心跳上、默认密码下不开放局域网（路线图 1.10，D63 第 6、8 条）。"""

import textwrap
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from sqlite3 import connect

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aureus.channels import WEB_CHANNEL_NAME
from aureus.common import AureusPaths, ConfigError, ManualClock, shutdown_logging
from aureus.core.runtime import open_core
from aureus.memory import (
    HistoricalMemory,
    ImportedLanguageRow,
    MemoryEntry,
    MemoryEvidence,
    MemorySource,
)
from aureus.memory.emoji import ImportedEmoji
from aureus.organs import CallPurpose, OrganRequest
from aureus.people import HistoricalKind, HistoricalRecord
from aureus.timeline import ActorType
from aureus.web import PasswordStore
from aureus.web.assemble import open_web
from aureus.web.runner import WebServer

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

IDENTITY = textwrap.dedent(
    """\
    [facts]
    name = "测试助手"
    birthday = "2008-01-01"

    [user]
    name = "测试用户"

    [persona]
    immutable = "你是一个测试用的数字生命。"
    """
)


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    return paths


def write_web_config(paths: AureusPaths, text: str) -> None:
    paths.runtime_config_file.write_text(f"[web]\n{text}", encoding="utf-8")


def test_a_disabled_web_channel_assembles_nothing(paths: AureusPaths) -> None:
    write_web_config(paths, "enabled = false\n")
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        with open_web(core, clock) as server:
            assert server is None
        # 没有接过网页通道：现在还能接。
        core.heartbeat.attach_channel(WEB_CHANNEL_NAME, lambda _message: None)


def test_an_enabled_web_channel_is_attached_to_the_heartbeat(paths: AureusPaths) -> None:
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert isinstance(server, WebServer)
        assert server.started is False  # 只是组装好，监听由调用方启动
        with pytest.raises(ValueError, match="已经接上"):
            core.heartbeat.attach_channel(WEB_CHANNEL_NAME, lambda _message: None)
    # 关闭后数据库连接都放掉了：Windows 上文件没被锁住，能删掉。
    paths.database_file.unlink()


def test_normal_web_start_reads_imports_and_manages_recall(paths: AureusPaths) -> None:
    password = "correct horse"
    PasswordStore(paths.web_password_file, FAST_HASHER).set_password(password)
    clock = ManualClock(START)
    source = MemorySource("demo", "memory", "paragraphs", (43,))
    evidence = MemoryEvidence("message", "demo:55")
    record = HistoricalMemory(source, "合成原件木卫二", evidence=(evidence,))
    emoji_source = MemorySource("demo", "main", "images", (4,))
    image = b"GIF89a"
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        assert core.memory and core.language and core.emoji_store
        digest = sha256(image).hexdigest()
        archive = paths.memory_dir / "attachments"
        archive.mkdir(exist_ok=True, parents=True)
        (archive / digest).write_bytes(image)
        core.emoji_store.import_records(
            [
                ImportedEmoji(
                    emoji_source, digest, "测试表情", True, False, 1, 3, None, None, "{}"
                ),
                ImportedEmoji(
                    MemorySource("demo", "main", "images", (5,)),
                    "a" * 64,
                    "缺件表情",
                    False,
                    True,
                    1,
                    0,
                    None,
                    None,
                    "{}",
                ),
            ]
        )
        core.memory.publish(
            [
                record,
                HistoricalMemory(
                    emoji_source,
                    "测试表情",
                    evidence=(MemoryEvidence("attachment_archived", f"attachments/{digest}"),),
                ),
            ]
        )
        core.language.publish(
            "demo",
            "manifest",
            {"jargons": 1},
            [
                ImportedLanguageRow(
                    "jargons",
                    "[7]",
                    {"id": 7},
                    "received",
                    "",
                    "灶台月",
                    "合成释义",
                    evidence=("[55]",),
                )
            ],
        )
        core.people.publish_history(
            [
                HistoricalRecord(
                    instance="demo",
                    kind=HistoricalKind.PROFILE_SNAPSHOT,
                    source_key="[3]",
                    summary="合成人物画像",
                    evidence=[55],
                )
            ]
        )
        live = core.timeline.append("message", "本地记录纸鸢", ActorType.USER)
        assert core.memory.index_more(100, lambda row: MemoryEntry("合成用户", row.content)) > 0
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert server is not None and core.memory and core.language
        app = server._server.config.app
        assert isinstance(app, FastAPI)
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            learning = "/api/resources/learning"
            memory = "/api/resources/memory"
            assert (
                client.patch(
                    memory, json={"kind": "live", "entry_id": live.id, "text": "未授权"}
                ).status_code
                == 401
            )
            assert client.get("/api/resources/media/" + digest).status_code == 401
            assert client.post("/api/login", json={"password": password}).status_code == 200
            assert client.get("/assets/resource-management.js").status_code == 200
            assert client.get("/assets/memory-flow-transfer.js").status_code == 200
            flow = client.get(memory + "/flow").json()["items"]
            [profile_event] = flow
            assert profile_event["event_type"] == "profile_snapshot"
            assert profile_event["source"]["key"] == [3]
            package = client.get(memory + "/transfer")
            assert package.status_code == 200 and b'"records":1' in package.content
            [jargon] = client.get(learning, params={"kind": "jargon"}).json()["items"]
            assert jargon["source_refs"] == ["[7]", "[55]"]
            assert "合成释义" in core.language.jargon_reference(["灶台月"], 500)
            target = {"kind": "jargon", "entry_key": jargon["entry_key"]}
            assert (
                client.patch(
                    learning,
                    json={
                        **target,
                        "content": {"text": "灶台月", "detail": "新释义"},
                        "is_enabled": False,
                    },
                ).status_code
                == 200
            )
            assert not core.language.jargon_reference(["灶台月"], 500)
            [hidden] = client.get(learning, params={"kind": "jargon", "recall": "disabled"}).json()[
                "items"
            ]
            assert hidden["original"][:2] == ["灶台月", "合成释义"]
            assert hidden["source_refs"] == jargon["source_refs"]
            assert client.patch(learning, json={**target, "is_enabled": True}).status_code == 200
            assert "新释义" in core.language.jargon_reference(["灶台月"], 500)
            assert (
                client.post(
                    learning,
                    json={
                        "kind": "expression",
                        "content": {"text": "平稳着陆", "detail": "合成跑步"},
                    },
                ).status_code
                == 201
            )
            assert "平稳着陆" in core.language.expression_reference("跑步", "", (), "", 500)

            payload = client.get(
                memory, params={"kind": "history", "source_table": "text", "recall": "enabled"}
            ).json()
            [history] = payload["items"]
            assert payload["counts"]["paragraphs"] == 1
            profile = client.get("/api/resources/profiles", params={"q": "合成"}).json()["items"][0]
            assert profile["record"]["source_key"] == "[3]"
            assert profile["record"]["evidence"] == [55]
            media = "/api/resources/media"
            [entry] = client.get(media, params={"status": "registered"}).json()["items"]
            assert entry["digest"] == digest and entry["has_file"] and entry["is_registered"]
            assert entry["source"] == emoji_source.encode()
            assert entry["use_count"] == 3
            [missing] = client.get(media, params={"status": "banned"}).json()["items"]
            assert missing["text"] == "缺件表情" and missing["digest"] is None
            assert not missing["has_file"] and missing["is_banned"]
            original = client.get(f"{media}/{digest}", params={"kind": "emoji"})
            assert original.headers["content-type"] == "image/gif" and original.content == image
            assert client.get(f"{media}/{'a' * 64}", params={"kind": "emoji"}).status_code == 404
            for kind, entry_id, old, revised in (
                ("history", history["entry_id"], "木卫二", "土卫六"),
                ("live", live.id, "纸鸢", "风筝架"),
            ):
                target_memory = {"kind": kind, "entry_id": entry_id}
                assert (
                    client.patch(memory, json={**target_memory, "text": revised}).status_code == 200
                )
                assert not core.memory.search([old], (), 10)
                [hit] = core.memory.search([revised], (), 10)
                assert hit.text == revised
                if kind == "history":
                    assert hit.source == source and hit.evidence == (evidence,)
                assert (
                    client.patch(memory, json={**target_memory, "is_enabled": False}).status_code
                    == 200
                )
                assert not core.memory.search([revised], (), 10)
                assert (
                    client.patch(memory, json={**target_memory, "is_enabled": True}).status_code
                    == 200
                )
                assert core.memory.search([revised], (), 10)
            assert core.memory.get_history(source) == record
    with closing(connect(paths.database_file)) as connection:
        connection.execute("DROP TABLE memory_edits_fts")
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        assert core.memory and core.language
        assert core.memory.search(["土卫六"], (), 10)[0].source == source
        assert core.memory.search(["风筝架"], (), 10)[0].record_id == live.id
        assert "新释义" in core.language.jargon_reference(["灶台月"], 500)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_organ", ["replyer", "custom-replyer"])
async def test_first_provider_save_keeps_fake_replies(paths: AureusPaths, reply_organ: str) -> None:
    paths.runtime_config_file.write_text(
        f'[heartbeat]\nreply_organ = "{reply_organ}"\n', encoding="utf-8"
    )
    password = "correct horse"
    PasswordStore(paths.web_password_file, FAST_HASHER).set_password(password)
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert server is not None
        app = server._server.config.app
        assert isinstance(app, FastAPI)
        client = TestClient(app, client=("127.0.0.1", 50000))
        assert client.post("/api/login", json={"password": password}).status_code == 200
        config = client.get("/api/models").json()["config"]
        assert not config["organs"]
        config["providers"] = [{"name": "local", "protocol": "openai"}]
        assert client.put("/api/models", json={"config": config}).status_code == 200
        response = await core.gateway.call(
            OrganRequest(organ_id="replyer", prompt="测试", purpose=CallPurpose.USER_REPLY)
        )
        assert response.metadata["type"] == "fake"
        assert response.attempts == []


def test_remote_access_with_the_default_password_refuses_to_assemble(paths: AureusPaths) -> None:
    write_web_config(paths, "allow_remote = true\n")
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        pytest.raises(ConfigError, match="allow_remote"),
        open_web(core, clock),
    ):
        pass


def test_remote_access_is_allowed_once_the_password_was_changed(paths: AureusPaths) -> None:
    PasswordStore(paths.web_password_file, FAST_HASHER).set_password("correct horse")
    write_web_config(paths, "allow_remote = true\n")
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert isinstance(server, WebServer)
