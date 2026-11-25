"""学习页原请求形状与实际持久状态。"""

from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from aureus.channels import WebChannel
from aureus.common import LanguageConfig
from aureus.memory import LanguageContent, LanguageKind
from aureus.memory.language_matching import JargonStatus
from aureus.web import create_app
from aureus.web.server import SESSION_COOKIE
from tests.web.test_expression_vector_api import VectorRig
from tests.web.test_expression_workbench_alias_api import _add_source
from tests.web.test_learning_workbench_api import _client
from tests.web.test_server import FakeHeartbeat


def test_summary_fields_follow_clock_and_current_records(tmp_path: Path) -> None:
    client, service, scopes = _client(tmp_path)
    with client:
        summary = "/api/webui/expression/stats/summary"
        assert client.get(summary).raise_for_status().json()["data"]["recent_7days"] == 0
        client.post("/api/webui/expression/", json={
            "style": "旧说法", "situation": "庆祝", "chat_id": "source-a",
        }).raise_for_status()
        service.clock.advance(timedelta(days=7))
        assert client.get(summary).raise_for_status().json()["data"]["recent_7days"] == 1
        service.clock.advance(timedelta(seconds=1))
        client.post("/api/webui/expression/", json={
            "style": "新说法", "situation": "庆祝", "chat_id": "source-b",
        }).raise_for_status()
        stats = client.get("/api/webui/expression/stats/summary").json()["data"]
        assert stats["total"] == 2 and stats["recent_7days"] == 1
        assert stats["chat_count"] == 2
        assert stats["top_chats"] == {"source-a": 1, "source-b": 1}
        del scopes["source-a"]
        stats = client.get("/api/webui/expression/stats/summary").json()["data"]
        assert stats["total"] == 1 and "source-a" not in stats["top_chats"]
        client.post("/api/webui/jargon/", json={
            "content": "合成词", "meaning": "表示完成", "session_id": "source-b",
        }).raise_for_status()
        stats = client.get("/api/webui/jargon/stats/summary").json()["data"]
        assert stats["confirmed_jargon"] == stats["manual_jargon"] == 1
        assert stats["confirmed_not_jargon"] == stats["pending"] == 0
        assert stats["complete_count"] == stats["chat_count"] == 1
        assert stats["global_count"] == 0


@pytest.mark.parametrize("revoke", [False, True])
def test_json_classification_rechecks_source_and_updates_same_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revoke: bool,
) -> None:
    client, service, scopes = _client(tmp_path)
    assert service.language is not None
    classify = service.language.classify_jargons

    def classify_after_scope_change(*args, **kwargs):
        del scopes["source-b"]
        return classify(*args, **kwargs)

    if revoke:
        monkeypatch.setattr(service.language, "classify_jargons", classify_after_scope_change)
    with client:
        jargon = client.post("/api/webui/jargon/", json={
            "content": "合成词", "meaning": "表示完成", "session_id": "source-b",
        }).raise_for_status().json()["data"]
        path = "/api/webui/jargon/batch/set-jargon"
        payload = {"ids": [jargon["id"]], "is_jargon": False}
        assert client.post(path, params=payload).status_code == 422
        [unchanged] = service.language.entries(LanguageKind.JARGON)
        assert unchanged.jargon_status == JargonStatus.CONFIRMED
        result = client.post(path, json=payload)
        if revoke:
            assert result.status_code == 404, result.text
        else:
            assert result.status_code == 200, result.text
            assert result.json()["changed"] == 1
            stats = client.get("/api/webui/jargon/stats/summary").json()["data"]
            assert stats["confirmed_not_jargon"] == 1
        rows = service.language.entries(LanguageKind.JARGON, include_disabled=True)
        assert rows[0].jargon_status == (
            JargonStatus.CONFIRMED if revoke else JargonStatus.NON_JARGON
        )
        if not revoke:
            [candidate] = service.language.entries(LanguageKind.JARGON)
            assert candidate.jargon_status == JargonStatus.NON_JARGON
            assert candidate.detail == "表示完成" and candidate.can_reference_jargon()


def test_single_emoji_delete_is_same_store_reversible_removal(tmp_path: Path) -> None:
    client, service, _scopes = _client(tmp_path)
    with Image.new("RGB", (8, 8), "orange") as image, BytesIO() as buffer:
        image.save(buffer, "PNG")
        original = buffer.getvalue()
    with client:
        uploaded = client.post("/api/webui/emoji/upload", files={
            "file": ("not-a-trusted-path.png", original, "image/png"),
        }, data={"description": "庆祝"})
        uploaded.raise_for_status()
        row = client.get("/api/webui/emoji/list").json()["data"][0]
        assert client.delete(f'/api/webui/emoji/{row["id"]}').status_code == 200
        assert service.emojis is not None
        assert service.emojis.read_sendable(uploaded.json()["digest"]) is None
        assert service.emojis.read_media(uploaded.json()["digest"]) == original
        assert service.emojis.set_removed([row["entry_key"]], False)
        assert service.emojis.read_sendable(uploaded.json()["digest"]) == original


@pytest.mark.parametrize(("legacy_learning_enabled", "recall_enabled", "reference_chars"), [
    (None, True, 700), (False, True, 700), (True, True, 0), (False, False, 700),
])
def test_chat_targets_use_runtime_language_settings_and_current_scope(
    tmp_path: Path,
    legacy_learning_enabled: bool | None,
    recall_enabled: bool,
    reference_chars: int,
) -> None:
    rig = VectorRig(tmp_path)
    try:
        allowed = _add_source(rig, "allowed")
        hidden = _add_source(rig, "hidden")
        rig.allowed.remove(hidden)
        key = allowed.language_scope_key()
        for kind in (LanguageKind.EXPRESSION, LanguageKind.JARGON):
            rig.language.add_entry(
                kind, LanguageContent("合成资料", "合成义项"), origin_scope=key,
                recorded_at=rig.web.clock.now(),
            )
        configuration = {"recall_enabled": recall_enabled, "reference_chars": reference_chars}
        if legacy_learning_enabled is not None:
            configuration["learning_enabled"] = legacy_learning_enabled
        app = create_app(
            config=rig.web.config,
            auth=rig.web.auth,
            sessions=rig.web.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=rig.web.timeline,
            resource_language=rig.language,
            inbox_store=rig.inbox,
            allows_inbox_conversation=rig.allowed.__contains__,
            learning_language_config=LanguageConfig.model_validate(configuration),
            clock=rig.web.clock,
        )
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE, rig.web.sessions.create())
            summary = client.get("/api/webui/expression/stats/summary").raise_for_status().json()
            assert summary["data"]["recent_7days"] == 1
            chats = client.get("/api/webui/expression/chats").raise_for_status().json()["data"]
            assert chats == client.get("/api/webui/expression/chat-targets").json()["data"]
            assert {row["platform"] for row in chats} == {"web", "terminal", "qq"}
            assert all(row["enable_learning"] is True for row in chats)
            assert all(
                row["use_expression"] == (recall_enabled and reference_chars > 0) for row in chats
            )
            group, = [row for row in chats if row["platform"] == "qq"]
            assert group == {
                "chat_id": key, "session_id": key, "chat_name": "合成群 allowed",
                "platform": "qq", "account_id": "bot", "target_id": "allowed", "is_group": True,
                "enable_learning": True,
                "use_expression": recall_enabled and reference_chars > 0,
                "expression_count": 1, "jargon_count": 0,
            }
            jargon = client.get("/api/webui/jargon/chats").json()["data"]
            assert next(row for row in jargon if row["session_id"] == key)["jargon_count"] == 1
            rig.allowed.clear()
            assert all(row["platform"] != "qq" for row in client.get(
                "/api/webui/expression/chat-targets"
            ).json()["data"])
    finally:
        rig.web.close()
