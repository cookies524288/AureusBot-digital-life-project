"""用合成语言包验证预览、去重、原件保护和运行召回。"""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import connect

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.memory.language import LanguageContent, LanguageKind, LanguagePackageRow, LanguageStore
from aureus.memory.language_matching import ExpressionReviewStatus, JargonStatus
from aureus.web.language_package_api import register_language_package_routes
from aureus.web.resource_api import register_resource_routes

ORIGIN = {"Origin": "http://testserver"}
LEARNING = "/api/resources/learning"
NOW = datetime(2026, 11, 23, tzinfo=UTC)


def _client(store: LanguageStore) -> TestClient:
    async def require_ready() -> str:
        return "test"

    def same_origin(request: Request) -> bool:
        return request.headers.get("Origin") == "http://testserver"

    app = FastAPI()
    clock = ManualClock(NOW)
    register_language_package_routes(app, require_ready, same_origin, store, clock)
    register_resource_routes(app, require_ready, store, None, same_origin=same_origin, clock=clock)
    return TestClient(app)


def test_legacy_expression_reviews_do_not_gate_candidates_or_export(tmp_path: Path) -> None:
    path = tmp_path / "language.db"
    store = LanguageStore(path)
    store.open()
    kind = LanguageKind.EXPRESSION
    target = store.add_entry(kind, LanguageContent("月兔收到", "月兔问候", "合成原件"))
    disabled = store.add_entry(kind, LanguageContent("月兔停用", "月兔问候"))
    assert store.update_entry(kind, disabled, None, False)
    package = LanguagePackageRow(LanguageContent("月兔未映射", "月兔问候"), 2, "合成群", "{}")
    assert store.save_package(kind, [package]) == 1
    unmapped = f"package:{package.digest}"
    endpoint = f"{LEARNING}/reviews"
    with closing(connect(path, autocommit=True)) as connection:
        connection.execute("DROP TABLE language_expression_reviews")
    store.open()
    with _client(store) as client:

        def entries(**params: str) -> dict[str, dict]:
            response = client.get(LEARNING, params={"kind": kind, **params})
            return {item["entry_key"]: item for item in response.json()["items"]}

        def reference(language: LanguageStore = store) -> str:
            return language.expression_reference("月兔问候", "打招呼", (), "合成人物")

        assert set(entries()) == {target, disabled, unmapped}
        assert "月兔收到" in reference()
        blocked = client.patch(endpoint, json={"entry_keys": [target], "status": "approved"})
        assert blocked.status_code == 404
        assert client.get(endpoint, params={"entry_key": target}).status_code == 404
        assert (
            store.review_expressions([target], ExpressionReviewStatus.REJECTED, "合成拒绝理由", NOW)
            == 1
        )
        rejected = entries()[target]
        assert rejected["is_enabled"] and not rejected["is_removed"]
        assert rejected["example"] == "合成原件"
        reopened = LanguageStore(path)
        reopened.open()
        assert "月兔收到" in reference(reopened)
        exported = client.get(f"{LEARNING}/package/expression").content.decode()
        assert "月兔收到" in exported
        assert store.set_removed(kind, [target], True)
        assert set(entries(removed="true")) == {target}
        assert not reference()
        assert "月兔收到" not in client.get(f"{LEARNING}/package/expression").content.decode()
        assert store.set_removed(kind, [target], False)
        assert "月兔收到" in reference()
        assert "月兔停用" not in reference() and "月兔未映射" not in reference()
        with closing(connect(path)) as connection:
            assert connection.execute(
                "SELECT status, reason FROM language_expression_reviews"
            ).fetchall() == [("rejected", "合成拒绝理由")]


@pytest.mark.parametrize("kind", list(LanguageKind))
def test_language_package_preview_round_trip_and_deduplication(
    tmp_path: Path, kind: LanguageKind
) -> None:
    store = LanguageStore(tmp_path / "language.db")
    store.open()
    store.add_entry(kind, LanguageContent("月兔", "本地释义", "合成例句"))
    spelling = "jargons" if kind == LanguageKind.JARGON else "expressions"
    fields = (
        {"include_chat_info": True}
        if kind == LanguageKind.JARGON
        else {"source_chat_name": "未映射的群"}
    )
    raw = (
        {
            "content": "月兔",
            "meaning": "原始释义",
            "count": 5,
            "is_jargon": True,
            "is_complete": True,
            "is_global": True,
            "targets": [{"platform": "qq", "id": "229", "type": "group", "count": 5}],
        }
        if kind == LanguageKind.JARGON
        else {
            "situation": "原始释义",
            "style": "月兔",
            "checked": False,
            "count": 5,
            "content_list": '["例句一", "例句二"]',
        }
    )
    package = {
        "type": f"maibot.{kind}.export",
        "version": 1,
        "exported_at": NOW.isoformat(),
        "count": 1,
        spelling: [raw],
        **fields,
    }
    endpoint = f"{LEARNING}/package/{kind}"
    with _client(store) as client:
        assert client.post(endpoint, json=package).status_code == 403
        preview = client.post(endpoint, json=package, headers=ORIGIN).json()
        assert preview["can_save"] == 1 and preview["can_recall"] == 0
        assert len(store.entries(kind, include_disabled=True)) == 1
        invalid = {**package, "count": 2, spelling: [raw, {}]}
        assert client.post(endpoint, json=invalid, headers=ORIGIN).status_code == 422
        changed = {**package, "version": 2}
        confirm = {"confirmed": preview["digest"]}
        assert (
            client.post(endpoint, params=confirm, json=changed, headers=ORIGIN).status_code == 409
        )
        assert len(store.entries(kind, include_disabled=True)) == 1
        imported = client.post(endpoint, params=confirm, json=package, headers=ORIGIN)
        assert imported.json() == {"added": 1, "skipped": 0, "can_recall": 0}
        duplicate = client.post(endpoint, params=confirm, json=package, headers=ORIGIN)
        assert duplicate.json() == {"added": 0, "skipped": 1, "can_recall": 0}
        [entry] = store.entries(kind)
        assert entry.detail == "本地释义"
        [pending] = [
            item for item in store.entries(kind, include_disabled=True) if item.pending_scope
        ]
        enable = {"kind": kind, "entry_key": pending.entry_key, "is_enabled": True}
        assert client.patch(LEARNING, json=enable, headers=ORIGIN).status_code == 422
        removal = {"kind": kind, "entry_keys": [pending.entry_key], "removed": True}
        assert client.patch(f"{LEARNING}/batch", json=removal, headers=ORIGIN).status_code == 200
        removal["removed"] = False
        assert client.patch(f"{LEARNING}/batch", json=removal, headers=ORIGIN).status_code == 200
        assert store.entries(kind) == [entry]
        assert store.update_entry(kind, pending.entry_key, LanguageContent("月兔", "覆盖"), None)
        exported = client.get(endpoint)
        assert exported.headers["Cache-Control"] == "no-store"
        assert exported.json()["type"] == package["type"]
        assert any(
            item.get("meaning", item.get("situation")) == "原始释义"
            for item in exported.json()[spelling]
        )

    destination = LanguageStore(tmp_path / "destination.db")
    destination.open()
    headers = {**ORIGIN, "Content-Type": "application/json"}
    with _client(destination) as client:
        preview = client.post(endpoint, content=exported.content, headers=headers).json()
        result = client.post(
            endpoint,
            content=exported.content,
            headers=headers,
            params={"confirmed": preview["digest"]},
        )
        assert result.json() == {"added": 2, "skipped": 0, "can_recall": 0}
        assert {value for value in store.package_originals(kind).values()} <= set(
            destination.package_originals(kind).values()
        )
        assert not destination.entries(kind)


def test_legacy_jargon_classification_survives_package_without_gating_meaning(
    tmp_path: Path,
) -> None:
    store = LanguageStore(tmp_path / "language.db")
    store.open()
    candidate = store.add_entry(LanguageKind.JARGON, LanguageContent("月兔", ""))
    second_candidate = store.add_entry(LanguageKind.JARGON, LanguageContent("白兔", ""))
    denied = store.add_entry(LanguageKind.JARGON, LanguageContent("月兔", "不作为黑话"))
    removed = store.add_entry(LanguageKind.JARGON, LanguageContent("月兔", "隐藏释义"))
    assert store.set_removed(LanguageKind.JARGON, [removed], True)
    endpoint = f"{LEARNING}/jargon/classification"
    with _client(store) as client:
        params = {"kind": "jargon", "jargon_status": "candidate", "shared": True}
        assert client.get(LEARNING, params=params).json()["total"] == 2
        body = {"entry_keys": [candidate, removed], "status": "non_jargon"}
        assert client.patch(endpoint, json=body).status_code == 403
        assert client.patch(endpoint, json=body, headers=ORIGIN).status_code == 404
        assert client.get(LEARNING, params=params).json()["total"] == 2
        body["entry_keys"] = [candidate, denied, candidate]
        assert client.patch(endpoint, json=body, headers=ORIGIN).json() == {
            "changed": 2,
            "status": "non_jargon",
        }
        assert (
            client.get(LEARNING, params=params).json()["items"][0]["entry_key"] == second_candidate
        )
        reference = store.jargon_reference(("月兔",))
        assert "月兔：不作为黑话" in reference and "隐藏释义" not in reference
        assert store.classify_jargons([denied], JargonStatus.CONFIRMED, NOW) == 1
        assert "月兔：不作为黑话" in store.jargon_reference(("月兔",))
        payload = client.get(f"{LEARNING}/package/jargon").content
    destination = LanguageStore(tmp_path / "destination.db")
    destination.open()
    with _client(destination) as client:
        headers = {**ORIGIN, "Content-Type": "application/json"}
        preview = client.post(f"{LEARNING}/package/jargon", content=payload, headers=headers).json()
        assert (
            client.post(
                f"{LEARNING}/package/jargon",
                content=payload,
                headers=headers,
                params={"confirmed": preview["digest"]},
            ).json()["added"]
            == 3
        )
        listed = client.get(LEARNING, params={"kind": "jargon"}).json()["items"]
        assert all(item["pending_scope"] for item in listed)
        assert sorted(item["jargon_status"] for item in listed) == [
            "candidate",
            "confirmed",
            "non_jargon",
        ]


@pytest.mark.parametrize("kind", list(LanguageKind))
def test_language_batch_restores_prior_recall_state(tmp_path: Path, kind: LanguageKind) -> None:
    store = LanguageStore(tmp_path / "language.db")
    store.open()
    active = store.add_entry(kind, LanguageContent("庆祝", "比赛", "原例句"))
    inactive = store.add_entry(kind, LanguageContent("祝福", "比赛"))
    assert store.update_entry(kind, inactive, None, False)

    def recalled() -> str:
        if kind == LanguageKind.JARGON:
            return store.jargon_reference(("庆祝和祝福",))
        return store.expression_reference("比赛", "庆祝", (), "测试者")

    assert "庆祝" in recalled() and "祝福" not in recalled()
    with _client(store) as client:
        batch = LEARNING + "/batch"
        payload = {"kind": kind, "entry_keys": [active, inactive], "removed": True}
        assert client.patch(batch, json=payload).status_code == 403
        bad = {**payload, "entry_keys": [active, "unknown"]}
        assert client.patch(batch, json=bad, headers=ORIGIN).status_code == 404
        assert "庆祝" in recalled()
        assert client.patch(batch, json=payload, headers=ORIGIN).status_code == 200
        assert not recalled()
        params = {"kind": kind, "removed": "true"}
        assert client.get(LEARNING, params=params).json()["total"] == 2
        assert (
            client.patch(batch, json={**payload, "removed": False}, headers=ORIGIN).status_code
            == 200
        )
        assert "庆祝" in recalled() and "祝福" not in recalled()
        assert store.entries(kind)[0].example == "原例句"
