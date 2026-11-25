"""学习页原请求形状与实际持久状态。"""

from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from aureus.memory import LanguageKind
from aureus.memory.language_matching import JargonStatus
from tests.web.test_learning_workbench_api import _client


def test_summary_fields_follow_clock_and_current_records(tmp_path: Path) -> None:
    client, service, scopes = _client(tmp_path)
    with client:
        client.post("/api/webui/expression/", json={
            "style": "旧说法", "situation": "庆祝", "chat_id": "source-a",
        }).raise_for_status()
        service.clock.advance(timedelta(days=8))
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
def test_query_classification_rechecks_source_and_updates_same_store(
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
        result = client.post(f'/api/webui/jargon/batch/set-jargon?ids={jargon["id"]}&is_jargon=false')
        if revoke:
            assert result.status_code == 404, result.text
        else:
            assert result.status_code == 200, result.text
            assert result.json()["changed"] == 1
            stats = client.get("/api/webui/jargon/stats/summary").json()["data"]
            assert stats["confirmed_not_jargon"] == 1
        rows = service.language.entries(LanguageKind.JARGON, include_disabled=True)
        assert rows[0].jargon_status == (JargonStatus.CONFIRMED if revoke else JargonStatus.NON_JARGON)
        if not revoke:
            assert service.language.entries(LanguageKind.JARGON) == []


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
