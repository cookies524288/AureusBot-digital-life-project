"""以合成表达快照验证网页独立授权及实时过滤。"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.memory import ExpressionClusterStore, LanguageContent, LanguageKind, LanguageStore
from aureus.memory.expression_clusters import build_expression_clusters
from aureus.memory.language import LanguagePackageRow
from aureus.memory.language_matching import ExpressionReviewStatus
from aureus.web import create_app
from aureus.web.expression_cluster_api import ExpressionClusterGrants
from aureus.web.server import SESSION_COOKIE
from tests.web.test_server import FakeHeartbeat, Rig, set_password

CLUSTERS = "/api/resources/learning/clusters"
DETAIL = CLUSTERS + "/detail"
EXPRESSION = LanguageKind.EXPRESSION
GROUP = "synthetic:group"


def _save(store: ExpressionClusterStore, language: LanguageStore, space: str, *keys: str) -> None:
    async def embed(texts: tuple[str, ...]) -> tuple[str, tuple[tuple[float, ...], ...]]:
        return space, ((1.0, 2.0),) * len(texts)

    snapshot = asyncio.run(
        build_expression_clusters(
            language, embed, cluster_count=1, authorized_entry_keys=keys, conversation_key=GROUP
        )
    )
    assert snapshot is not None
    store.save(snapshot)


@pytest.fixture
def fixture(
    tmp_path: Path,
) -> Iterator[tuple[Rig, LanguageStore, ExpressionClusterStore, dict[str, str]]]:
    rig = Rig(tmp_path)
    try:
        language = LanguageStore(tmp_path / "synthetic-language.sqlite")
        language.open()
        store = ExpressionClusterStore(tmp_path / "synthetic-clusters.sqlite")
        keys = {
            name: language.add_entry(EXPRESSION, LanguageContent(name, "合成情景", "不公开的例句"))
            for name in ("可见甲", "可见乙", "隐藏", "停用", "移除", "拒绝", "修改")
        }
        package = LanguagePackageRow(LanguageContent("包成员", "包情景"), 1, "合成来源", "{}")
        assert language.save_package(EXPRESSION, [package]) == 1
        package_key = f"package:{package.digest}"
        keys["待映射"] = package_key
        assert language.package_scope(EXPRESSION, package_key, [GROUP], confirm=True) == ()
        _save(
            store,
            language,
            "synthetic:first",
            *[key for name, key in keys.items() if name != "可见乙"],
        )
        _save(store, language, "synthetic:second", keys["可见乙"])
        _save(store, language, "synthetic:invisible", keys["隐藏"])
        assert language.update_entry(EXPRESSION, keys["停用"], None, False)
        assert language.set_removed(EXPRESSION, [keys["移除"]], True)
        assert (
            language.review_expressions(
                [keys["拒绝"]], ExpressionReviewStatus.REJECTED, "合成拒绝", rig.clock.now()
            )
            == 1
        )
        assert language.update_entry(
            EXPRESSION, keys["修改"], LanguageContent("更新", "更新"), None
        )
        assert language.package_scope(EXPRESSION, package_key, [], confirm=True) == (GROUP,)
        yield rig, language, store, keys
    finally:
        rig.close()


def _client(
    rig: Rig,
    language: LanguageStore,
    store: ExpressionClusterStore,
    grants: ExpressionClusterGrants | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            config=rig.config,
            auth=rig.auth,
            sessions=rig.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=rig.timeline,
            resource_language=language,
            expression_cluster_store=store,
            expression_cluster_grants=grants,
        )
    )


def test_cluster_requests_require_ready_and_do_not_infer_grants(
    fixture: tuple[Rig, LanguageStore, ExpressionClusterStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, language, store, keys = fixture

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("无独立网页授权时不得查快照或写入")

    monkeypatch.setattr(store, "browse", forbidden)
    monkeypatch.setattr(store, "save", forbidden)
    assert keys["可见甲"] in {entry.entry_key for entry in language.expression_candidates(GROUP)}
    with _client(rig, language, store) as client:
        assert client.get(CLUSTERS).status_code == 401
        assert client.get(DETAIL).status_code == 401
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        assert client.get(CLUSTERS).status_code == 403
        set_password(rig)
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        response = client.get(CLUSTERS, params={"allowed_entry_keys": keys["可见甲"]})
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"items": [], "total": 0, "next_offset": None}
        assert client.get(DETAIL, params={"location": "f" * 64}).status_code == 404
        assert client.post(CLUSTERS).status_code == 405


def test_cluster_filtering_precedes_counts_and_detail_rechecks_state(
    fixture: tuple[Rig, LanguageStore, ExpressionClusterStore, dict[str, str]],
) -> None:
    rig, language, store, keys = fixture
    set_password(rig)
    allowed = set(keys.values()) - {keys["隐藏"]}
    with _client(rig, language, store, lambda _token: allowed) as client:
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        first = client.get(CLUSTERS, params={"limit": 1}).json()
        second = client.get(CLUSTERS, params={"limit": 1, "offset": 1}).json()
        assert first["total"] == second["total"] == 2
        assert first["next_offset"] == 1 and second["next_offset"] is None
        [left], [right] = first["items"], second["items"]
        assert (left["number"], left["title"], left["count"]) == (1, "可见甲", 1)
        assert (right["number"], right["title"], right["count"]) == (2, "可见乙", 1)
        assert left["location"] != right["location"]
        detail = client.get(DETAIL, params={"location": left["location"]})
        assert detail.headers["cache-control"] == "no-store"
        assert detail.json()["members"] == [{"text": "可见甲", "detail": "合成情景"}]
        assert client.get(DETAIL, params={"location": right["location"]}).json()["members"] == [
            {"text": "可见乙", "detail": "合成情景"}
        ]
        missing = client.get(DETAIL, params={"location": "bad"})
        allowed.remove(keys["可见甲"])
        assert client.get(CLUSTERS).json()["total"] == 1
        revoked = client.get(DETAIL, params={"location": left["location"]})
        assert revoked.status_code == missing.status_code == 404
        assert revoked.content == missing.content
        _save(store, language, "synthetic:second", keys["可见乙"])
        assert client.get(DETAIL, params={"location": right["location"]}).content == missing.content


def test_cluster_member_pages_use_filtered_total_and_reject_invalid_limits(
    fixture: tuple[Rig, LanguageStore, ExpressionClusterStore, dict[str, str]],
) -> None:
    rig, language, store, keys = fixture
    set_password(rig)
    members = {}
    for index in range(7):
        text = f"可见成员{index}"
        members[language.add_entry(EXPRESSION, LanguageContent(text, "合成情景"))] = text
    hidden = keys["隐藏"]
    _save(store, language, "synthetic:first", hidden, *members)
    with _client(rig, language, store, lambda _token: members.keys()) as client:
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        [cluster] = client.get(CLUSTERS).json()["items"]
        params = {"location": cluster["location"], "limit": 3}
        first, second, last, past_end = [
            client.get(DETAIL, params={**params, "offset": offset}).json()
            for offset in (0, 3, 6, 9)
        ]
        assert [page["total"] for page in (first, second, last, past_end)] == [7] * 4
        assert [(page["offset"], page["limit"]) for page in (first, second, last, past_end)] == [
            (0, 3),
            (3, 3),
            (6, 3),
            (9, 3),
        ]
        assert [page["next_offset"] for page in (first, second, last, past_end)] == [
            3,
            6,
            None,
            None,
        ]
        assert [len(page["members"]) for page in (first, second, last, past_end)] == [3, 3, 1, 0]
        assert [member["text"] for page in (first, second, last) for member in page["members"]] == [
            members[key] for key in sorted(members)
        ]
        for invalid in ({"offset": -1}, {"limit": 0}, {"limit": 101}):
            assert client.get(DETAIL, params={**params, **invalid}).status_code == 422


def test_cluster_inflight_revocation_discards_list_and_detail(
    fixture: tuple[Rig, LanguageStore, ExpressionClusterStore, dict[str, str]],
) -> None:
    rig, language, store, keys = fixture
    set_password(rig)
    allowed = {keys["可见甲"]}
    [cluster] = store.browse(language, allowed_entry_keys=allowed)
    calls = 0

    def changing_grants(_token: str) -> set[str]:
        nonlocal calls
        calls += 1
        return allowed if calls % 2 else set()

    with _client(rig, language, store, changing_grants) as client:
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        page = client.get(CLUSTERS)
        assert page.status_code == 409 and "可见甲" not in page.text
        detail = client.get(DETAIL, params={"location": cluster.location})
        assert detail.status_code == 404 and "可见甲" not in detail.text
        assert calls == 4
