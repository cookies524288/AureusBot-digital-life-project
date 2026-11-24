"""原工作台契约通过同一资料库进入真实语言与表情读侧。"""

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

from aureus.common import ManualClock
from aureus.memory import EmojiStore, LanguageKind, LanguageStore
from aureus.organs import OrganRequest, OrganResponse
from aureus.web.learning_workbench_api import register_learning_workbench_routes
from aureus.web.learning_workbench_service import LearningWorkbench
from aureus.web.text_learning import register_text_learning_routes


def _client(tmp_path: Path) -> tuple[TestClient, LearningWorkbench, dict[str, str]]:
    clock = ManualClock(datetime(2026, 11, 24, tzinfo=UTC))
    database = tmp_path / "shared.db"
    language = LanguageStore(database)
    language.open()
    emojis = EmojiStore(database, tmp_path / "media", clock)
    emojis.open()
    scopes = {"source-a": "合成来源甲", "source-b": "合成来源乙"}
    service = LearningWorkbench(language, emojis, clock, lambda: dict(scopes))

    async def ready(request: Request) -> str:
        if request.cookies.get("session") != "fixture":
            raise HTTPException(401)
        return "fixture"

    async def extract(request: OrganRequest) -> OrganResponse:
        assert request.organ_id == "summarizer"
        assert request.context["task"] == "text_learning_expression"
        return OrganResponse(
            request.organ_id,
            json.dumps(
                {
                    "candidates": [
                        {"title": "庆祝", "description": "月兔冲线", "source_id": 1},
                        {"title": "庆祝", "description": "月兔冲线", "source_id": 1},
                    ]
                }
            ),
        )

    def origin(request: Request) -> bool:
        return request.headers.get("origin") == "http://testserver"

    def active(token: str) -> bool:
        return token == "fixture"

    app = FastAPI()
    register_learning_workbench_routes(app, service, ready, origin, active)
    register_text_learning_routes(app, ready, origin, active, extract)
    client = TestClient(app)
    client.cookies.set("session", "fixture")
    client.headers["Origin"] = "http://testserver"
    return client, service, scopes


def test_expression_crud_ids_paging_restart_and_source_revocation(tmp_path: Path) -> None:
    client, service, scopes = _client(tmp_path)
    prefix = "/api/webui/expression"
    with client:
        created = client.post(
            prefix + "/",
            json={
                "situation": "庆祝",
                "style": "月兔冲线",
                "chat_id": "source-a",
            },
        )
        assert created.status_code == 200, created.text
        entry = created.json()["data"]
        identifier = entry["id"]
        assert entry["create_date"] == service.clock.now().timestamp()
        assert entry["checked"] is False
        assert service.language is not None
        assert "月兔冲线" in service.language.expression_reference(
            "庆祝", "庆祝", (), "fixture", 2000, conversation_key="source-b"
        )
        client.post(prefix + "/", json={"situation": "庆祝", "style": "第二条"})
        page = client.get(prefix + "/list", params={"page_size": 1}).json()
        assert page["total"] == 2 and len(page["data"]) == 1
        filtered = client.get(prefix + "/list", params={"chat_id": "source-a"}).json()
        assert filtered["total"] == 1 and filtered["data"][0]["id"] == identifier
        assert client.patch(prefix + f"/{identifier}", json={"style": "月兔起飞"}).is_success
        assert client.get(prefix + f"/{identifier}").json()["data"]["style"] == "月兔起飞"
        restarted = LanguageStore(tmp_path / "shared.db")
        restarted.open()
        assert restarted.resolve_workbench_ids(LanguageKind.EXPRESSION, [identifier]) == {
            identifier: entry["entry_key"]
        }
        del scopes["source-a"]
        assert client.get(prefix + f"/{identifier}").status_code == 404
        assert client.patch(prefix + f"/{identifier}", json={"style": "不可改"}).status_code == 404
        scopes["source-a"] = "重新授权"
        assert client.delete(prefix + f"/{identifier}").is_success
        assert client.get(prefix + f"/{identifier}").status_code == 404
        removed = client.get(prefix + "/list", params={"removed": True}).json()
        assert removed["data"][0]["id"] == identifier
        assert service.language.set_removed(LanguageKind.EXPRESSION, [entry["entry_key"]], False)
        assert client.get(prefix + f"/{identifier}").is_success


def test_jargon_classification_is_atomic_and_used_by_runtime(tmp_path: Path) -> None:
    client, service, _scopes = _client(tmp_path)
    with client:
        result = client.post("/api/webui/jargon/", json={"content": "月兔", "meaning": "合成队伍"})
        assert result.is_success, result.text
        identifier = result.json()["data"]["id"]
        assert service.language is not None
        assert "月兔" in service.language.jargon_reference(["月兔"], 2000)
        response = client.post(
            "/api/webui/jargon/batch/set-jargon",
            json={
                "ids": [identifier, identifier + 999],
                "is_jargon": False,
            },
        )
        assert response.status_code == 404
        assert "月兔" in service.language.jargon_reference(["月兔"], 2000)
        assert client.post(
            "/api/webui/jargon/batch/set-jargon",
            json={
                "ids": [identifier],
                "is_jargon": False,
            },
        ).is_success
        assert "月兔" not in service.language.jargon_reference(["月兔"], 2000)
        assert client.post(
            "/api/webui/jargon/batch/set-jargon",
            json={
                "ids": [identifier],
                "is_jargon": True,
            },
        ).is_success
        assert "月兔" in service.language.jargon_reference(["月兔"], 2000)


def test_emoji_review_is_independent_and_persistent(tmp_path: Path) -> None:
    client, service, _scopes = _client(tmp_path)
    assert service.emojis is not None
    with Image.new("RGB", (8, 8), "red") as image, BytesIO() as buffer:
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
    digest, _ = service.emojis.upload(data, registration_description="合成庆祝")
    with client:
        row = client.get("/api/webui/emoji/list").json()["data"][0]
        identifier = row["id"]
        assert row["full_path"].startswith("/api/webui/emoji/")
        assert client.patch(
            f"/api/webui/emoji/{identifier}/review",
            json={
                "verdict": "rejected",
            },
        ).is_success
        assert service.emojis.is_registered(digest)
        assert service.emojis.read_sendable(digest) is None
        reopened = EmojiStore(tmp_path / "shared.db", tmp_path / "media", service.clock)
        reopened.open()
        assert reopened.read_sendable(digest) is None
        assert client.patch(
            f"/api/webui/emoji/{identifier}/review",
            json={
                "verdict": "approved",
            },
        ).is_success
        assert reopened.read_sendable(digest) == data
        assert client.get(f"/api/webui/emoji/{identifier}/thumbnail").content == data


def test_auth_cross_origin_and_text_extraction_have_no_implicit_write(tmp_path: Path) -> None:
    client, service, _scopes = _client(tmp_path)
    with client:
        client.cookies.clear()
        assert client.get("/api/webui/expression/list").status_code == 401
        client.cookies.set("session", "fixture")
        client.headers["Origin"] = "https://outside.invalid"
        assert (
            client.post(
                "/api/webui/expression/",
                json={
                    "situation": "庆祝",
                    "style": "月兔",
                },
            ).status_code
            == 403
        )
        client.headers["Origin"] = "http://testserver"
        source = "合成正文\n" * 1000
        response = client.post("/api/webui/learning/text/prepare", json={"text": source})
        assert response.is_success, response.text
        chunks = response.json()["chunks"]
        assert "".join(chunks) == source and all(len(part) <= 4500 for part in chunks)
        result = client.post(
            "/api/webui/learning/text/extract",
            json={
                "kind": "expression",
                "text": "合成原文",
            },
        )
        assert result.json() == {
            "candidates": [
                {"title": "庆祝", "description": "月兔冲线", "evidence": "合成原文"},
            ]
        }
        assert service.language is not None
        assert service.language.entries(LanguageKind.EXPRESSION, include_disabled=True) == []
