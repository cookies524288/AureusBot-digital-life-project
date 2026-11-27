"""原工作台契约通过同一资料库进入真实语言与表情读侧。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from sqlite3 import Connection

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

import aureus.memory.language as language_module
from aureus.common import ManualClock
from aureus.memory import EmojiStore, LanguageContent, LanguageKind, LanguageProposal, LanguageStore
from aureus.memory.language import LanguagePackageRow, LanguageSample
from aureus.memory.language_import import EXPRESSION_TABLE, ImportedLanguageRow
from aureus.memory.language_matching import ExpressionReviewStatus
from aureus.organs import OrganRequest, OrganResponse
from aureus.timeline import TimelineDatabase
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


def _trace_language_sql(monkeypatch: pytest.MonkeyPatch, trace: Callable[[str], None]) -> None:
    open_connection = language_module.open_connection

    def traced_connection(path: Path) -> Connection:
        connection = open_connection(path)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(language_module, "open_connection", traced_connection)


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
            "庆祝", "庆祝", (), "fixture", conversation_key="source-b"
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
        assert client.get(prefix + f"/{identifier}").status_code == 403
        assert client.patch(prefix + f"/{identifier}", json={"style": "不可改"}).status_code == 403
        scopes["source-a"] = "重新授权"
        assert client.delete(prefix + f"/{identifier}").is_success
        assert client.get(prefix + f"/{identifier}").status_code == 404
        removed = client.get(prefix + "/list", params={"removed": True}).json()
        assert removed["data"][0]["id"] == identifier
        assert service.language.set_removed(LanguageKind.EXPRESSION, [entry["entry_key"]], False)
        assert client.get(prefix + f"/{identifier}").is_success


def test_expression_list_filters_and_pages_all_authoritative_sources_in_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service, scopes = _client(tmp_path)
    store = service.language
    assert store is not None
    kind = LanguageKind.EXPRESSION
    timeline = TimelineDatabase(tmp_path / "shared.db", service.clock)
    timeline.open()
    timeline.close()
    sample = LanguageSample("synthetic:1", 1, "person:test", "Straße 观察说法")
    store.observe([sample], [LanguageProposal(kind, "Straße 观察说法", "庆祝", (1,))])
    store.observe_extraction(
        kind,
        "source-b",
        [sample],
        {1: sample.content},
        [LanguageProposal(kind, "观察说法", "Straße 在线场景", (1,))],
    )
    store.publish(
        "合成实例",
        "fixture",
        {EXPRESSION_TABLE: 2},
        [
            ImportedLanguageRow(
                EXPRESSION_TABLE, "旧行", {}, "received", text="Straße 历史说法", detail="旧场景"
            ),
            ImportedLanguageRow(EXPRESSION_TABLE, "待处理", {}, "pending", text="Straße 不可见"),
        ],
    )
    mapped = LanguagePackageRow(LanguageContent("Straße 包记录", "包场景"), 1, "测试", "{}")
    pending = LanguagePackageRow(LanguageContent("Straße 待映射", "待映射场景"), 1, "测试", "[]")
    assert store.save_package(kind, [mapped, pending]) == 2
    assert (
        store.package_scope(
            kind, f"package:{mapped.digest}", ["source-b", "source-a"], confirm=True
        )
        == ()
    )
    manual = store.add_entry(
        kind,
        LanguageContent("旧原文", "手写场景"),
        origin_scope="source-a",
        recorded_at=service.clock.now(),
    )
    assert store.update_entry(kind, manual, LanguageContent("Straße 100%", "新场景"), False)
    assert (
        store.review_expressions([manual], ExpressionReviewStatus.APPROVED, "", service.clock.now())
        == 1
    )
    removed_key = store.add_entry(kind, LanguageContent("Straße 已移除", "旧场景"))
    assert store.set_removed(kind, [removed_key], True)
    old_id = store.workbench_ids(kind, [manual])[manual]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("表达列表不能装载全部权威条目或全部工作台 payload")

    statements: list[str] = []
    monkeypatch.setattr(service, "language_payloads", forbidden)
    monkeypatch.setattr(store, "entries", forbidden)
    _trace_language_sql(monkeypatch, statements.append)
    endpoint = "/api/webui/expression/list"
    with client:
        pages = [
            client.get(endpoint, params={"page": page, "page_size": 2, "search": "STRASSE"})
            .raise_for_status()
            .json()
            for page in (1, 2, 3)
        ]
        assert [page["total"] for page in pages] == [6, 6, 6]
        assert [len(page["data"]) for page in pages] == [2, 2, 2]
        rows = [row for page in pages for row in page["data"]]
        assert [row["id"] for row in rows] == sorted({row["id"] for row in rows}, reverse=True)
        assert {row["style"] for row in rows} == {
            "Straße 观察说法",
            "观察说法",
            "Straße 历史说法",
            "Straße 包记录",
            "Straße 待映射",
            "Straße 100%",
        }
        by_style = {row["style"]: row for row in rows}
        assert by_style["Straße 历史说法"]["entry_key"] == json.dumps(
            [EXPRESSION_TABLE, "合成实例", "旧行"], separators=(",", ":")
        )
        assert not by_style["Straße 待映射"]["is_enabled"]
        assert by_style["Straße 待映射"]["chat_name"] == "共享资料"
        assert by_style["Straße 包记录"]["chat_id"] == "source-a"
        edited = by_style["Straße 100%"]
        assert edited["id"] == old_id and not edited["is_enabled"] and edited["checked"]
        assert edited["situation"] == "新场景"
        assert edited["create_date"] == service.clock.now().timestamp()
        assert any("SELECT count(*) FROM matched" in query for query in statements)
        assert any("LIMIT 2 OFFSET 2" in query for query in statements)
        assert sum("INSERT INTO learning_workbench_ids" in query for query in statements) == 1

        filtered = client.get(endpoint, params={"chat_id": "source-a", "search": "%"}).json()
        assert filtered["total"] == 1 and filtered["data"] == [edited]
        removed = client.get(endpoint, params={"removed": True}).json()
        assert removed["total"] == 1 and removed["data"][0]["style"] == "Straße 已移除"
        assert not removed["data"][0]["is_enabled"]
        del scopes["source-a"]
        filtered = client.get(
            endpoint, params={"chat_id": "source-b", "sort_by": "time_asc"}
        ).json()
        assert [row["style"] for row in filtered["data"]] == ["观察说法", "Straße 包记录"]
        assert filtered["data"][1]["id"] == by_style["Straße 包记录"]["id"]
        empty = client.get(endpoint, params={"page": 10**20, "search": "STRASSE"}).json()
        assert empty["total"] == 5 and empty["data"] == []
        del scopes["source-b"]
        shared = client.get(endpoint).raise_for_status().json()
        assert {row["style"] for row in shared["data"]} == {
            "Straße 观察说法",
            "Straße 历史说法",
            "Straße 待映射",
        }


def test_expression_page_reports_authorization_changed_during_sql_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service, scopes = _client(tmp_path)
    store = service.language
    assert store is not None
    store.add_entry(
        LanguageKind.EXPRESSION,
        LanguageContent("私有合成条目", "合成场景"),
        origin_scope="source-a",
        recorded_at=service.clock.now(),
    )

    def revoke_during_query(statement: str) -> None:
        if "LIMIT 20 OFFSET 0" in statement:
            del scopes["source-a"]

    _trace_language_sql(monkeypatch, revoke_during_query)
    with client:
        result = client.get("/api/webui/expression/list")
    assert result.status_code == 403
    assert result.json() == {"detail": "来源授权已变化，请刷新后重试。"}


def test_jargon_classification_is_atomic_and_used_by_runtime(tmp_path: Path) -> None:
    client, service, _scopes = _client(tmp_path)
    with client:
        result = client.post("/api/webui/jargon/", json={"content": "月兔", "meaning": "合成队伍"})
        assert result.is_success, result.text
        identifier = result.json()["data"]["id"]
        assert service.language is not None
        assert "月兔" in service.language.jargon_reference(["月兔"])
        response = client.post(
            "/api/webui/jargon/batch/set-jargon",
            json={"ids": [identifier, identifier + 999], "is_jargon": False},
        )
        assert response.status_code == 404
        assert "月兔" in service.language.jargon_reference(["月兔"])
        assert client.post(
            "/api/webui/jargon/batch/set-jargon",
            json={"ids": [identifier], "is_jargon": False},
        ).is_success
        assert "月兔：合成队伍" in service.language.jargon_reference(["月兔"])
        assert client.post(
            "/api/webui/jargon/batch/set-jargon",
            json={"ids": [identifier], "is_jargon": True},
        ).is_success
        assert "月兔" in service.language.jargon_reference(["月兔"])


def test_workbench_import_export_skip_conflicts_and_preserve_selected_scope(tmp_path: Path) -> None:
    client, _service, scopes = _client(tmp_path)
    prefix = "/api/webui/expression"
    with client:
        request = {
            "chat_id": "source-a",
            "expressions": [
                {
                    "situation": "庆祝",
                    "style": "月兔冲线",
                    "content_list": '["合成证据"]',
                    "count": 3,
                    "checked": False,
                    "modified_by": None,
                }
            ],
        }
        imported = client.post(prefix + "/import", json=request)
        assert imported.status_code == 200, imported.text
        assert imported.json()["imported_count"] == 1
        repeated = client.post(prefix + "/import", json=request)
        assert repeated.status_code == 200 and repeated.json()["imported_count"] == 0
        page = client.get(prefix + "/list", params={"chat_id": "source-a"}).json()
        assert page["total"] == 1 and page["data"][0]["style"] == "月兔冲线"
        exported = client.post(prefix + "/export", json={"chat_id": "source-a"})
        assert exported.status_code == 200
        package = json.loads(exported.content)
        assert package["count"] == 1 and package["expressions"][0]["style"] == "月兔冲线"
        assert exported.headers["content-disposition"].endswith('filename="aureus-expression.json"')
        assert client.post(prefix + "/export", json={"chat_id": "not-known"}).status_code == 422
        scopes.pop("source-a")
        assert client.post(prefix + "/export", json={"chat_id": "source-a"}).status_code == 422
        assert client.post(prefix + "/import", json=request).status_code == 422


def test_batch_removal_is_atomic_and_rechecks_current_scope(tmp_path: Path) -> None:
    client, _service, scopes = _client(tmp_path)
    prefix = "/api/webui/expression"
    with client:
        identifiers = []
        for style in ("第一说法", "第二说法"):
            response = client.post(
                prefix + "/",
                json={
                    "situation": "庆祝",
                    "style": style,
                    "chat_id": "source-a",
                },
            )
            identifiers.append(response.json()["data"]["id"])
        scopes.pop("source-a")
        denied = client.post(prefix + "/batch/delete", json={"ids": identifiers})
        assert denied.status_code == 403
        scopes["source-a"] = "恢复授权"
        assert client.get(prefix + "/list").json()["total"] == 2
        assert client.post(prefix + "/batch/delete", json={"ids": identifiers}).is_success
        assert client.get(prefix + "/list").json()["total"] == 0
        assert client.get(prefix + "/list", params={"removed": True}).json()["total"] == 2


def test_expression_import_reaches_reference_and_export_survives_reopen(tmp_path: Path) -> None:
    client, service, _scopes = _client(tmp_path)
    assert service.language is not None
    prefix = "/api/webui/expression"
    with client:
        imported = client.post(
            prefix + "/import",
            json={
                "chat_id": "source-a",
                "expressions": [
                    {
                        "situation": "庆祝比赛",
                        "style": "我们赢啦",
                        "content_list": '["合成证据"]',
                        "count": 3,
                        "last_active_time": None,
                        "create_time": None,
                        "checked": False,
                        "modified_by": None,
                    }
                ],
            },
        )
        assert imported.status_code == 200, imported.text
        row = client.get(prefix + "/list", params={"chat_id": "source-a"}).json()["data"][0]
        assert "我们赢啦" in service.language.expression_reference(
            "比赛庆祝", "庆祝", (), "speaker", conversation_key="source-a"
        )
        exported = client.post(prefix + "/export", json={"chat_id": "source-a"})
        assert exported.status_code == 200
        assert json.loads(exported.content)["expressions"][0]["style"] == "我们赢啦"
        reopened = LanguageStore(tmp_path / "shared.db")
        reopened.open()
        assert reopened.resolve_workbench_ids(LanguageKind.EXPRESSION, [row["id"]]) == {
            row["id"]: row["entry_key"]
        }
        assert "我们赢啦" in reopened.expression_reference(
            "比赛庆祝", "庆祝", (), "speaker", conversation_key="source-a"
        )


def test_jargon_import_reaches_reference_and_export_survives_reopen(tmp_path: Path) -> None:
    client, _service, _scopes = _client(tmp_path)
    prefix = "/api/webui/jargon"
    with client:
        imported = client.post(
            prefix + "/import",
            json={
                "target_session_ids": ["source-b"],
                "conflict_strategy": "skip",
                "jargons": [
                    {
                        "content": "合成梗",
                        "meaning": "测试中表示完成",
                        "count": 8,
                        "is_jargon": True,
                        "is_complete": True,
                        "is_global": False,
                        "created_by": "MANUAL",
                    }
                ],
            },
        )
        assert imported.status_code == 200, imported.text
        [row] = client.get(prefix + "/list", params={"session_id": "source-b"}).json()["data"]
        assert "合成梗" in LanguageStore(tmp_path / "shared.db").jargon_reference(
            ["合成梗"], conversation_key="source-b"
        )
        exported = client.post(prefix + "/export", json={"ids": [row["id"]]})
        assert exported.status_code == 200
        assert json.loads(exported.content)["jargons"][0]["meaning"] == "测试中表示完成"
        reopened = LanguageStore(tmp_path / "shared.db")
        reopened.open()
        assert reopened.resolve_workbench_ids(LanguageKind.JARGON, [row["id"]]) == {
            row["id"]: row["entry_key"]
        }
        assert "合成梗" in reopened.jargon_reference(["合成梗"], conversation_key="source-b")


def test_revoked_scope_blocks_import_export_clear_and_delete_without_changes(
    tmp_path: Path,
) -> None:
    client, service, scopes = _client(tmp_path)
    assert service.language is not None
    prefix = "/api/webui/expression"
    with client:
        created = client.post(
            prefix + "/",
            json={
                "situation": "庆祝",
                "style": "保留说法",
                "chat_id": "source-a",
            },
        )
        row = created.json()["data"]
        before = service.language.entries(LanguageKind.EXPRESSION, include_disabled=True)
        del scopes["source-a"]
        assert (
            client.post(
                prefix + "/import",
                json={
                    "chat_id": "source-a",
                    "expressions": [{"situation": "庆祝", "style": "不应导入"}],
                },
            ).status_code
            == 422
        )
        assert client.post(prefix + "/export", json={"chat_id": "source-a"}).status_code == 422
        assert client.post(prefix + "/clear", json={"chat_id": "source-a"}).status_code == 422
        assert client.delete(prefix + f"/{row['id']}").status_code == 403
        assert service.language.entries(LanguageKind.EXPRESSION, include_disabled=True) == before
        scopes["source-a"] = "重新授权"
        assert client.post(prefix + "/export", json={"ids": []}).status_code == 422
        result = (
            client.post(
                prefix + "/import",
                json={
                    "chat_id": "source-a",
                    "conflict_strategy": "overwrite",
                    "expressions": [
                        {"situation": "庆祝", "style": "保留说法", "content_list": '["新例句"]'}
                    ],
                },
            )
            .raise_for_status()
            .json()
        )
        assert result["imported_count"] == 1
        updated = next(
            entry
            for entry in service.language.entries(LanguageKind.EXPRESSION)
            if entry.entry_key == row["entry_key"]
        )
        assert updated.example == "新例句"


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
        thumbnail = f"/api/webui/emoji/{identifier}/thumbnail"
        with Image.open(BytesIO(client.get(thumbnail).raise_for_status().content)) as preview:
            assert preview.format == "WEBP" and preview.size == (8, 8)
        assert client.get(thumbnail, params={"original": True}).content == data


def test_emoji_upload_register_ban_remove_stats_and_cache_contract(tmp_path: Path) -> None:
    client, service, _scopes = _client(tmp_path)
    with Image.new("RGB", (8, 8), "blue") as image, BytesIO() as buffer:
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
    with client:
        uploaded = client.post(
            "/api/webui/emoji/upload",
            files={"file": ("fixture.png", data, "image/png")},
            data={"description": "庆祝,蓝色"},
        )
        assert uploaded.status_code == 200, uploaded.text
        digest = uploaded.json()["digest"]
        rows = client.get("/api/webui/emoji/list").json()["data"]
        [row] = [item for item in rows if item["emoji_hash"] == digest]
        identifier = row["id"]
        assert row["is_registered"] is True
        assert client.patch(
            f"/api/webui/emoji/{identifier}/review",
            json={
                "verdict": "rejected",
            },
        ).is_success
        assert client.post(f"/api/webui/emoji/{identifier}/ban").is_success
        assert service.emojis is not None and service.emojis.read_sendable(digest) is None
        assert client.post(f"/api/webui/emoji/{identifier}/register").status_code == 422
        assert client.get(f"/api/webui/emoji/{identifier}/thumbnail").is_success
        assert client.post(
            "/api/webui/emoji/batch/delete", json={"emoji_ids": [identifier]}
        ).is_success
        assert client.get(f"/api/webui/emoji/{identifier}").status_code == 404
        removed = client.get("/api/webui/emoji/list", params={"removed": True}).json()
        assert any(item["id"] == identifier for item in removed["data"])
        stats = client.get("/api/webui/emoji/stats/summary")
        assert stats.status_code == 200 and stats.json()["data"]["discarded"] == 1
        cache = "/api/webui/emoji/thumbnail-cache"
        assert client.get(cache + "/stats").raise_for_status().json()["total_count"] == 1
        assert client.post(cache + "/cleanup").raise_for_status().json()["cleaned_count"] == 1
        assert client.get(cache + "/stats").raise_for_status().json()["total_count"] == 0
        assert service.emojis.read_media(digest) == data
        assert client.post("/api/webui/emoji/batch/upload", content=b"unused").status_code == 422


def test_emoji_upload_accepts_exact_dashboard_form_data_contract(tmp_path: Path) -> None:
    client, service, _scopes = _client(tmp_path)
    with Image.new("RGB", (8, 8), "green") as image, BytesIO() as buffer:
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
    with client:
        uploaded = client.post(
            "/api/webui/emoji/upload",
            files={"file": ("fixture.png", data, "image/png")},
            data={"description": "轻松,庆祝"},
        )
        assert uploaded.status_code == 200, uploaded.text
        body = uploaded.json()
        assert body["created"] and body["registered"]
        assert service.emojis is not None
        [entry] = [
            entry
            for entry in service.emojis.browse_page("", 0, 10)[0]
            if entry.digest == body["digest"]
        ]
        assert entry.description == "轻松,庆祝"
        assert entry.tags == ("轻松", "庆祝")
        assert service.emojis.read_catalog_media(body["digest"]) == data


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
