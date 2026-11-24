"""用合成图谱验证只读查询的登录和独立授权边界。"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.memory import (
    HistoricalMemory,
    MemoryGraphEvidence,
    MemoryGraphSource,
    MemoryIndex,
    MemorySource,
)
from aureus.web import create_app
from aureus.web.memory_graph_api import GraphGrants
from aureus.web.server import SESSION_COOKIE
from tests.web.test_server import FakeHeartbeat, Rig, set_password

GRAPH = "/api/resources/memory/graph"
DETAIL = GRAPH + "/detail"
ACCESS = GRAPH + "/access"
REVISION = "X-Aureus-Graph-Revision"
LEFT = MemorySource("合成实例", "memory", "entities", ("left",))
RIGHT = MemorySource("合成实例", "memory", "entities", ({"blob": "42"},))
EDGE = MemorySource("合成实例", "memory", "relations", ("edge",))
HIDDEN = MemorySource("合成实例", "memory", "entities", ("hidden",))
EVIDENCE = MemorySource("合成实例", "memory", "paragraphs", ("evidence",))


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[tuple[Rig, MemoryIndex]]:
    rig = Rig(tmp_path)
    memory = MemoryIndex(tmp_path / "aureus.db", rig.timeline, rig.clock)
    memory.open()
    try:
        memory.publish(
            [
                HistoricalMemory(source, "合成原件")
                for source in (LEFT, RIGHT, EDGE, HIDDEN, EVIDENCE)
            ],
            graph_sources=(
                MemoryGraphSource(LEFT, "entity", "active", name="蓝鲸甲"),
                MemoryGraphSource(RIGHT, "entity", "active", name="蓝鲸乙"),
                MemoryGraphSource(HIDDEN, "entity", "active", name="蓝鲸"),
                MemoryGraphSource(
                    EDGE,
                    "relation",
                    "active",
                    predicate="观察蓝鲸",
                    subject_source=LEFT,
                    object_source=RIGHT,
                    evidence=(MemoryGraphEvidence("证据", EVIDENCE),),
                ),
            ),
        )
        yield rig, memory
    finally:
        memory.close()
        rig.close()


def _client(rig: Rig, memory: MemoryIndex, grants: GraphGrants | None = None) -> TestClient:
    return TestClient(
        create_app(
            config=rig.config,
            auth=rig.auth,
            sessions=rig.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=rig.timeline,
            resource_memory=memory,
            memory_graph_grants=grants,
        )
    )


def test_default_scope_never_accepts_client_grants_and_requires_ready_session(
    graph: tuple[Rig, MemoryIndex],
) -> None:
    rig, memory = graph
    with _client(rig, memory) as client:
        assert client.get(GRAPH).status_code == 401
        assert client.get(DETAIL, params={"source": LEFT.encode()}).status_code == 401
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        assert client.get(GRAPH).status_code == 403
        assert client.get(DETAIL, params={"source": LEFT.encode()}).status_code == 403
        set_password(rig)
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        response = client.get(GRAPH, params={"allowed_sources": LEFT.encode(), "q": "蓝鲸"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json() == {"covered": False, "items": [], "total": 0, "next_offset": None}
        assert client.get(DETAIL, params={"source": LEFT.encode()}).status_code == 404
        assert client.post(GRAPH).status_code == 405
        rig.clock.advance(timedelta(days=rig.config.session_days))
        assert client.get(GRAPH).status_code == 401
        assert client.get(DETAIL, params={"source": LEFT.encode()}).status_code == 401


def test_granted_browse_counts_before_paging_and_hides_unauthorized_detail(
    graph: tuple[Rig, MemoryIndex],
) -> None:
    rig, memory = graph
    set_password(rig)
    grants = [LEFT, RIGHT, EDGE]
    with _client(rig, memory, lambda _token: grants) as client:
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        first = client.get(GRAPH, params={"q": "蓝鲸", "limit": 1}).json()
        assert first["covered"] and first["total"] == 3 and first["next_offset"] == 1
        assert [item["source"] for item in first["items"]] == [RIGHT.encode()]
        second = client.get(GRAPH, params={"q": "蓝鲸", "offset": 1, "limit": 1}).json()
        assert second["total"] == 3 and second["next_offset"] == 2
        assert [item["source"] for item in second["items"]] == [LEFT.encode()]
        empty = client.get(GRAPH, params={"q": "没有命中"}).json()
        assert empty == {"covered": True, "items": [], "total": 0, "next_offset": None}
        neighbors = client.get(GRAPH, params={"neighbor_of": RIGHT.encode()}).json()
        relation = client.get(DETAIL, params={"source": EDGE.encode()}).json()
        assert neighbors["items"] == [relation]
        assert relation["subject"]["source"] == LEFT.encode()
        assert relation["object"]["source"] == RIGHT.encode()
        assert relation["evidence"] == []
        missing = client.get(DETAIL, params={"source": HIDDEN.encode()})
        assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
        for source in ("invalid", '["合成实例","memory","entities",["unknown"]]'):
            response = client.get(DETAIL, params={"source": source})
            assert response.status_code == missing.status_code
            assert response.content == missing.content
        grants.remove(RIGHT)
        assert client.get(DETAIL, params={"source": EDGE.encode()}).content == missing.content
        assert client.get(GRAPH, params={"neighbor_of": RIGHT.encode()}).json()["covered"] is False


def test_page_with_inflight_revocation_never_returns_old_grants(
    graph: tuple[Rig, MemoryIndex],
) -> None:
    rig, memory = graph
    set_password(rig)
    calls = 0

    def changing_grants(_token: str) -> tuple[MemorySource, ...]:
        nonlocal calls
        calls += 1
        return (LEFT,) if calls % 2 else ()

    with _client(rig, memory, changing_grants) as client:
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        page = client.get(GRAPH)
        assert page.status_code == 409 and "蓝鲸" not in page.text
        detail = client.get(DETAIL, params={"source": LEFT.encode()})
        assert detail.status_code == 404 and "蓝鲸" not in detail.text


def test_graph_access_revision_tracks_sources_and_session_without_exposing_contents(
    graph: tuple[Rig, MemoryIndex],
) -> None:
    rig, memory = graph
    set_password(rig)
    grants = [LEFT, RIGHT, EDGE]
    with _client(rig, memory, lambda _token: grants) as client:
        assert client.get(ACCESS).status_code == 401
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        page = client.get(GRAPH)
        revision = page.headers[REVISION]
        access = client.get(ACCESS)
        assert access.json() == {} and access.headers["cache-control"] == "no-store"
        assert access.headers[REVISION] == revision
        assert client.get(DETAIL, params={"source": EDGE.encode()}).headers[REVISION] == revision
        grants.reverse()
        assert client.get(ACCESS).headers[REVISION] == revision
        grants.remove(EDGE)
        assert client.get(ACCESS).headers[REVISION] != revision
        grants.append(EDGE)
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        assert client.get(ACCESS).headers[REVISION] != revision
