"""以同库 ASGI 请求核对原学习页簇与向量响应。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.core.embedding_progress import EmbeddingProgress
from aureus.heartbeat import MessageRoute, Perception, TrustLevel
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.memory import LanguageContent
from aureus.timeline import VisibilityScope
from aureus.web import create_app
from aureus.web.expression_cluster_grants import ExpressionClusterDisplay
from aureus.web.server import SESSION_COOKIE
from tests.web.test_expression_vector_api import (
    ACTIVE, EXPRESSION, ORIGIN, PREVIOUS, SPACES, VectorRig, _finished,
)
from tests.web.test_server import FakeHeartbeat

WORKBENCH = "/api/webui/expression"
CLUSTERS = WORKBENCH + "/clusters"
LEGACY_CLUSTERS = "/api/resources/learning/clusters"
VECTORS = WORKBENCH + "/runtime/vectors/spaces"
PROGRESS = WORKBENCH + "/vector-build-progress"


@pytest.fixture
def workbench(tmp_path: Path):
    rig = VectorRig(tmp_path)
    try:
        display = ExpressionClusterDisplay(tmp_path / "aureus.db", rig.language)
        progress = EmbeddingProgress(
            rig.models, True, rig.language, rig.index, inbox=rig.inbox,
            allows_conversation=rig.allowed.__contains__,
        )
        app = create_app(
            config=rig.web.config, auth=rig.web.auth, sessions=rig.web.sessions,
            channel=WebChannel(FakeHeartbeat()), timeline=rig.web.timeline,
            resource_language=rig.language, expression_cluster_display=display,
            inbox_store=rig.inbox, allows_inbox_conversation=rig.allowed.__contains__,
            embedding_progress=progress, expression_recall=rig.recall,
        )
        with TestClient(app) as client:
            yield rig, display, client
    finally:
        rig.web.close()


def _add_source(rig: VectorRig, target: str) -> ConversationKey:
    heard = Perception(
        "qq", "synthetic-speaker", "合成来源", VisibilityScope.GROUP,
        TrustLevel.VERIFIED_CONTACT, MessageRoute("bot", "group", target, "1"),
    )
    rig.inbox.add(heard, conversation_name=f"合成群 {target}")
    route = ConversationKey.from_perception(heard)
    rig.allowed.add(route)
    return route


def test_workbench_clusters_map_granted_original_not_matching_text(workbench) -> None:
    rig, display, client = workbench
    alpha, beta = (_add_source(rig, name) for name in ("alpha", "beta"))
    scopes = frozenset(route.language_scope_key() for route in (alpha, beta))
    wanted, private = (
        rig.language.add_entry(
            EXPRESSION, LanguageContent("同一写法", "同一情景"),
            origin_scope=scope, recorded_at=rig.web.clock.now(),
        )
        for scope in (alpha.language_scope_key(), beta.language_scope_key())
    )
    entries = rig.language.entries(EXPRESSION)
    rig.index.publish(ACTIVE, entries, ((1.0, 2.0),) * len(entries))
    version, _ = display.selection(wanted, scopes)
    assert version is not None and display.set_grant(wanted, version, scopes, lambda: True)
    assert client.get(CLUSTERS).status_code == 401
    client.cookies.set(SESSION_COOKIE, rig.web.sessions.create())
    assert client.get(CLUSTERS, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    result = client.get(CLUSTERS).raise_for_status()
    assert result.headers["cache-control"] == "no-store"
    [cluster] = result.json()["clusters"]
    assert cluster["size"] == result.json()["sample_count"] == 1
    assert cluster["members"] == []
    path = f'{CLUSTERS}/{cluster["cluster_id"]}/members'
    params = {"profile_marker": cluster["embedding_profile_marker"]}
    response = client.get(path, params=params).raise_for_status()
    [member] = response.json()["data"]
    assert response.json()["cluster"]["members"] == [member]
    assert member == {
        "id": member["id"], "situation": "同一情景", "style": "同一写法", "count": 0,
        "chat_id": alpha.language_scope_key(), "chat_name": "合成群 alpha",
        "checked": False, "modified_by": None,
    }
    assert member["id"] > 0
    assert client.get(f'{WORKBENCH}/{member["id"]}').json()["data"]["entry_key"] == wanted
    assert private not in response.text and wanted not in response.text
    location = client.get(LEGACY_CLUSTERS).json()["items"][0]["location"]
    legacy = client.get(LEGACY_CLUSTERS + "/detail", params={"location": location}).json()
    assert legacy["members"] == [{"text": "同一写法", "detail": "同一情景"}]
    rig.allowed.remove(alpha)
    assert client.get(path, params=params).status_code == 404
    assert client.get(CLUSTERS).json()["clusters"] == []


@pytest.mark.parametrize("change", ["grant", "session"])
def test_workbench_cluster_rejects_in_flight_visibility_change(workbench, monkeypatch, change) -> None:
    rig, display, client = workbench
    key = rig.add(1)
    rig.index.publish(ACTIVE, rig.language.entries(EXPRESSION), ((1.0, 2.0),))
    scopes = frozenset()
    version, _ = display.selection(key, scopes)
    assert version is not None and display.set_grant(key, version, scopes, lambda: True)
    token = rig.web.sessions.create()
    client.cookies.set(SESSION_COOKIE, token)
    [cluster] = client.get(CLUSTERS).json()["clusters"]
    identify = rig.language.workbench_ids

    def identify_then_revoke(kind, keys):
        result = identify(kind, keys)
        if change == "grant":
            assert display.set_grant(key, None, scopes, lambda: True)
        else:
            rig.web.sessions.revoke(token)
        return result

    monkeypatch.setattr(rig.language, "workbench_ids", identify_then_revoke)
    request = client.get(f'{CLUSTERS}/{cluster["cluster_id"]}/members', params={
        "profile_marker": cluster["embedding_profile_marker"],
    })
    assert request.status_code == (404 if change == "grant" else 401)
    assert "合成表达" not in request.text


def test_workbench_vector_progress_reads_live_coverage_and_actual_build(workbench) -> None:
    rig, _display, client = workbench
    rig.add(1)
    first, = rig.language.entries(EXPRESSION)
    rig.index.publish(PREVIOUS, [first], ((1.0, 0.0),))
    rig.add(2)
    client.cookies.set(SESSION_COOKIE, rig.web.sessions.create())
    spaces = client.get(VECTORS).raise_for_status().json()
    assert spaces["success"] and spaces["target_space_id"] == ACTIVE
    current, previous = spaces["items"]
    assert current["vector_count"] == current["size_bytes"] == 0
    assert previous == {
        "space_id": PREVIOUS, "state": "saved", "can_delete": False,
        "embedding_fingerprint": {"hash": PREVIOUS, "dimension": 2},
        "vector_count": 1, "size_bytes": len(b"[1.0, 0.0]"), "last_used_at": None,
    }
    assert client.get(PROGRESS).json() == {"status": "pending", "completed": 0, "total": 2, "percent": 0}
    rig.wait = True
    try:
        assert client.post(SPACES + "/backfill", headers=ORIGIN, json={"space": ACTIVE}).status_code == 202
        assert rig.started.wait(2)
        assert client.get(PROGRESS).json()["status"] == "running"
        assert client.get(VECTORS).json()["items"][0]["state"] == "syncing"
    finally:
        rig.release.set()
    assert _finished(client, "written")["written"] == 2
    assert client.get(PROGRESS).json() == {"status": "completed", "completed": 2, "total": 2, "percent": 100}
    assert client.get(VECTORS).json()["items"][0]["vector_count"] == 2
