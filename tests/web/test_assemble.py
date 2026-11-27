"""网页服务组装的测试：开关、接到心跳上、默认密码下不开放局域网（路线图 1.10，D63 第 6、8 条）。"""

import textwrap
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from sqlite3 import connect
from threading import Event
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aureus.channels import WEB_CHANNEL_NAME
from aureus.common import AureusPaths, ConfigError, ManualClock, shutdown_logging
from aureus.core.runtime import open_core
from aureus.heartbeat import EVENT_MESSAGE
from aureus.memory import (
    ExpressionVectorIndex,
    HistoricalMemory,
    ImportedLanguageRow,
    LanguageContent,
    LanguageKind,
    MemoryEntry,
    MemoryEvidence,
    MemoryGraphSource,
    MemorySource,
)
from aureus.memory.emoji import ImportedEmoji
from aureus.organs import CallPurpose, OrganRequest
from aureus.people import HistoricalKind, HistoricalRecord
from aureus.timeline import ActorType, TimelineQuery, TimelineRecord
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


@pytest.mark.parametrize("limit", [10, 0])
def test_web_connections_share_the_inbound_limit_and_only_record_accepted_messages(
    paths: AureusPaths, limit: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = "correct horse"
    PasswordStore(paths.web_password_file, FAST_HASHER).set_password(password)
    write_web_config(paths, f"inbound_queue_limit = {limit}\n")
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert server is not None
        app = server._server.config.app
        assert isinstance(app, FastAPI)
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            assert client.portal is not None
            assert client.post("/api/login", json={"password": password}).status_code == 200
            origin = {"Origin": "http://testserver"}
            with (
                client.websocket_connect("/ws", headers=origin) as first,
                client.websocket_connect("/ws", headers=origin) as second,
            ):
                accepted: dict[str, str] = {}
                rejected: list[str] = []
                for number in range(1, 7):
                    first.send_json({"text": f"甲 {number}", "request_id": number})
                    second.send_json({"text": f"乙 {number}", "request_id": number})
                for label, socket in (("甲", first), ("乙", second)):
                    for number in range(1, 7):
                        receipt = socket.receive_json()
                        assert receipt["request_id"] == number
                        text = f"{label} {number}"
                        if receipt["type"] == "accepted":
                            accepted[text] = receipt["message_key"]
                        else:
                            assert receipt["type"] == "error"
                            assert receipt["code"] == "inbound_queue_full"
                            assert "没有收到" in receipt["detail"]
                            rejected.append(text)
                assert len(accepted) == (limit or 12)
                assert len(set(accepted.values())) == len(accepted)
                assert client.get("/api/history").json() == []

                started, release = Event(), Event()
                write_record = core.timeline._write_record

                def hold_write(*args: Any, **kwargs: Any) -> TimelineRecord:
                    started.set()
                    assert release.wait(timeout=5), "测试未按时释放时间线写线程"
                    return write_record(*args, **kwargs)

                monkeypatch.setattr(core.timeline, "_write_record", hold_write)
                pending = client.portal.start_task_soon(core.heartbeat._take_in_perceptions)
                try:
                    assert started.wait(timeout=5), "第一条来信未提交时间线写线程"
                    assert client.get("/api/history").json() == []
                    second.send_json({"text": "写入期间到达", "request_id": 7})
                    receipt = second.receive_json()
                    assert receipt["request_id"] == 7
                    assert not pending.done()
                    if limit:
                        assert receipt["type"] == "error"
                        assert receipt["code"] == "inbound_queue_full"
                        rejected.append("写入期间到达")
                    else:
                        assert receipt["type"] == "accepted"
                        accepted["写入期间到达"] = receipt["message_key"]
                finally:
                    release.set()
                    pending.result(timeout=5)

                first.send_json({"text": "已经写入但还没处理", "request_id": 8})
                receipt = first.receive_json()
                assert receipt["request_id"] == 8
                if limit:
                    assert receipt["code"] == "inbound_queue_full"
                    rejected.append("已经写入但还没处理")
                else:
                    assert receipt["type"] == "accepted"
                    accepted["已经写入但还没处理"] = receipt["message_key"]

                client.portal.call(core.heartbeat._take_in_perceptions)
                history = client.get("/api/history").json()
                assert {row["content"]: row["message_key"] for row in history} == accepted
                records = core.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=25))
                assert {record.content for record in records} == accepted.keys()
                assert not set(rejected).intersection(accepted)


def test_normal_web_connects_published_graph_display_grants(paths: AureusPaths) -> None:
    password = "correct horse"
    PasswordStore(paths.web_password_file, FAST_HASHER).set_password(password)
    clock = ManualClock(START)
    source = MemorySource("demo", "memory", "entities", (43,))
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert server is not None and core.memory is not None
        core.memory.publish(
            [HistoricalMemory(source, "只供测试")],
            snapshot=("demo", "synthetic-fingerprint"),
            graph_sources=[MemoryGraphSource(source, "entity", "active", name="合成图节点")],
        )
        app = server._server.config.app
        assert isinstance(app, FastAPI)
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            assert client.post("/api/login", json={"password": password}).status_code == 200
            graph = "/api/resources/memory/graph"
            assert client.get(graph).json()["total"] == 0
            catalog = client.get(graph + "/grants").json()
            assert catalog["enabled"]
            [batch] = catalog["items"]
            assert batch["instance"] == "demo" and not batch["categories"]
            result = client.put(
                graph + "/grants/" + batch["batch_id"],
                headers={"Origin": "http://testserver"},
                json={"password": password, "categories": ["entities"]},
            )
            assert result.status_code == 200
            [node] = client.get(graph).json()["items"]
            assert node["name"] == "合成图节点"


def test_normal_web_grants_and_revokes_only_selected_expression(paths: AureusPaths) -> None:
    password = "correct horse"
    PasswordStore(paths.web_password_file, FAST_HASHER).set_password(password)
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert server is not None and core.language is not None
        language = core.language
        key = language.add_entry(LanguageKind.EXPRESSION, LanguageContent("获准表达", "合成情景"))
        language.add_entry(LanguageKind.EXPRESSION, LanguageContent("未授权表达", "另一情景"))
        entries = language.expression_candidates(None)
        index = ExpressionVectorIndex(paths.database_file)
        index.publish("synthetic", entries, ((1.0, 2.0), (1.0, 2.0)))
        app = server._server.config.app
        assert isinstance(app, FastAPI)
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            clusters = "/api/resources/learning/clusters"
            grants = clusters + "/grants"
            origin = {"Origin": "http://testserver"}
            assert client.get(grants, params={"entry_key": key}).status_code == 401
            assert client.post("/api/login", json={"password": password}).status_code == 200
            assert client.get(clusters).json() == {"items": [], "total": 0, "next_offset": None}
            selected = client.get(grants, params={"entry_key": key}).json()
            assert selected["enabled"] and not selected["granted"] and selected["version"]
            body = {"entry_key": key, "version": selected["version"], "password": password}
            assert client.put(grants, json=body).status_code == 403
            stale = client.put(grants, headers=origin, json={**body, "version": "0" * 64})
            assert stale.status_code == 409
            assert client.put(grants, headers=origin, json=body).status_code == 200
            [cluster] = client.get(clusters).json()["items"]
            assert cluster["count"] == 1 and cluster["title"] == "获准表达"
            detail = clusters + "/detail"
            params = {"location": cluster["location"]}
            visible = client.get(detail, params=params)
            assert visible.headers["cache-control"] == "no-store"
            assert visible.json()["members"] == [{"text": "获准表达", "detail": "合成情景"}]
            revoked = client.put(grants, headers=origin, json={**body, "version": None})
            assert revoked.status_code == 200
            assert client.get(clusters).json() == {"items": [], "total": 0, "next_offset": None}
            old = client.get(detail, params=params)
            unknown = client.get(detail, params={"location": "0" * 64})
            assert old.status_code == unknown.status_code == 404 and old.content == unknown.content
            assert language.expression_candidates(None) == entries
            assert client.put(grants, headers=origin, json=body).status_code == 200
            assert language.update_entry(
                LanguageKind.EXPRESSION, key, LanguageContent("获准表达", "合成  情景"), None
            )
            index.publish("synthetic", language.expression_candidates(None), ((1.0, 2.0),) * 2)
            assert client.get(clusters).json()["total"] == 0
            assert client.get(detail, params=params).content == unknown.content


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
        message = core.timeline.append("message", "聊天原文钴蓝鹭", ActorType.USER)
        live = core.timeline.append("experience", "本地记录纸鸢", ActorType.USER)
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
            assert (
                client.patch(
                    memory, json={"kind": "live", "entry_id": message.id, "text": "不应写入"}
                ).status_code
                == 404
            )
            assert not core.memory.search(["钴蓝鹭"], (), 10)
            assert not core.memory.search(["不应写入"], (), 10)
            flow = client.get(memory + "/flow").json()["items"]
            [profile_event] = flow
            assert profile_event["event_type"] == "profile_snapshot"
            assert profile_event["source"]["key"] == [3]
            package = client.get(memory + "/transfer")
            assert package.status_code == 200 and b'"records":1' in package.content
            [jargon] = client.get(learning, params={"kind": "jargon"}).json()["items"]
            assert jargon["source_refs"] == ["[7]", "[55]"]
            assert "合成释义" in core.language.jargon_reference(["灶台月"])
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
            assert not core.language.jargon_reference(["灶台月"])
            [hidden] = client.get(learning, params={"kind": "jargon", "recall": "disabled"}).json()[
                "items"
            ]
            assert hidden["original"][:2] == ["灶台月", "合成释义"]
            assert hidden["source_refs"] == jargon["source_refs"]
            assert client.patch(learning, json={**target, "is_enabled": True}).status_code == 200
            assert "新释义" in core.language.jargon_reference(["灶台月"])
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
            assert "平稳着陆" in core.language.expression_reference("跑步", "", (), "")

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
        assert "新释义" in core.language.jargon_reference(["灶台月"])


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
