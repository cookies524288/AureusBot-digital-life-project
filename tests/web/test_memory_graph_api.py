"""用合成图谱验证只读查询的登录和独立授权边界。"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from aureus.channels import WebChannel
from aureus.memory import (
    HistoricalMemory,
    MemoryGraphEvidence,
    MemoryGraphPage,
    MemoryGraphSource,
    MemoryIndex,
    MemorySource,
)
from aureus.web import create_app
from aureus.web.memory_graph_api import GraphGrants
from aureus.web.server import SESSION_COOKIE
from tests.web.test_server import GOOD, FakeHeartbeat, Rig, set_password

GRAPH = "/api/resources/memory/graph"
DETAIL = GRAPH + "/detail"
ACCESS = GRAPH + "/access"
GRANTS = GRAPH + "/grants"
REVISION = "X-Aureus-Graph-Revision"
SAME_ORIGIN = {"Origin": "http://testserver"}
LEFT = MemorySource("合成实例", "memory", "entities", ("left",))
RIGHT = MemorySource("合成实例", "memory", "entities", ({"blob": "42"},))
EDGE = MemorySource("合成实例", "memory", "relations", ("edge",))
HIDDEN = MemorySource("合成实例", "memory", "entities", ("hidden",))
EVIDENCE = MemorySource("合成实例", "memory", "paragraphs", ("evidence",))
LINK = MemorySource("合成实例", "memory", "paragraph_relations", ("link",))


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


def _client(
    rig: Rig, memory: MemoryIndex, grants: GraphGrants | None = None, *, formal: bool = False
) -> TestClient:
    return TestClient(
        create_app(
            config=rig.config,
            auth=rig.auth,
            sessions=rig.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=rig.timeline,
            resource_memory=memory,
            memory_graph_grants=grants,
            memory_graph_display=memory.graph_display if formal else None,
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


def _publish_trusted(memory: MemoryIndex, *, include_extra: bool = False) -> None:
    sources = [LEFT, RIGHT, EDGE, EVIDENCE, LINK]
    projection = [
        MemoryGraphSource(LEFT, "entity", "active", name="合成左端"),
        MemoryGraphSource(RIGHT, "entity", "active", name="合成右端"),
        MemoryGraphSource(
            EDGE, "relation", "active", predicate="合成关系",
            subject_source=LEFT, object_source=RIGHT,
            evidence=(MemoryGraphEvidence("合成证据", EVIDENCE, LINK),),
        ),
    ]
    if include_extra:
        sources.append(HIDDEN)
        projection.append(MemoryGraphSource(HIDDEN, "entity", "active", name="后来增加"))
    memory.publish(
        (HistoricalMemory(source, "可信原件") for source in sources),
        snapshot=(LEFT.instance, "合成发布指纹"),
        graph_sources=projection,
    )


def _write_grants(
    client: TestClient, batch: str, *categories: str, password: str = GOOD
) -> Response:
    return client.put(
        f"{GRANTS}/{batch}", headers=SAME_ORIGIN,
        json={"password": password, "categories": categories},
    )


def test_formal_grants_require_trusted_publication_and_password(
    graph: tuple[Rig, MemoryIndex],
) -> None:
    rig, memory = graph
    set_password(rig)
    with _client(rig, memory, formal=True) as client:
        assert client.get(GRANTS).status_code == 401
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        assert client.get(GRANTS).json() == {"enabled": True, "items": []}
        assert _write_grants(client, LEFT.encode(), "entities").status_code == 404

        _publish_trusted(memory)
        catalog = client.get(GRANTS)
        [available] = catalog.json()["items"]
        batch_id = available["batch_id"]
        assert available == {"batch_id": batch_id, "instance": LEFT.instance, "categories": []}
        assert catalog.headers["cache-control"] == "no-store"
        assert client.get(GRAPH).json()["total"] == 0
        assert client.put(f"{GRANTS}/{batch_id}", json={
            "password": GOOD, "categories": ["entities"],
        }).status_code == 403
        assert _write_grants(client, batch_id, LEFT.encode()).status_code == 422
        assert _write_grants(client, batch_id, "entities", password="wrong").status_code == 403
        assert client.get(GRAPH).json()["total"] == 0

        assert _write_grants(client, batch_id, "relations", "evidence").status_code == 200
        assert client.get(GRAPH).json()["total"] == 0
        assert _write_grants(client, batch_id, "entities").status_code == 200
        assert client.get(GRAPH).json()["total"] == 2
        assert client.get(DETAIL, params={"source": EDGE.encode()}).status_code == 404
        assert client.get(DETAIL, params={"source": HIDDEN.encode()}).status_code == 404
        assert _write_grants(client, batch_id, "entities", "relations").status_code == 200
        assert client.get(DETAIL, params={"source": EDGE.encode()}).json()["evidence"] == []
        full_grant = _write_grants(client, batch_id, "entities", "relations", "evidence")
        assert full_grant.status_code == 200
        relation = client.get(DETAIL, params={"source": EDGE.encode()})
        assert relation.json()["evidence"] == [EVIDENCE.encode()]
        assert client.get(DETAIL, params={"source": EVIDENCE.encode()}).status_code == 404
        assert client.get(GRAPH, params={"neighbor_of": LEFT.encode()}).json()["items"] == [
            relation.json()
        ]
        assert _write_grants(client, batch_id, password="wrong").status_code == 403
        assert _write_grants(client, batch_id).status_code == 200
        empty = client.get(GRAPH, params={"neighbor_of": LEFT.encode()})
        assert empty.json() == {"covered": False, "items": [], "total": 0, "next_offset": None}
        assert empty.headers[REVISION] != relation.headers[REVISION]
        assert client.get(DETAIL, params={"source": EDGE.encode()}).status_code == 404
        assert client.get(GRANTS).json()["items"][0]["categories"] == []
        assert memory.search_graph_recall(["合成左端"], 5)


def test_formal_batch_is_persistent_and_does_not_expand_or_accept_changed_sources(
    graph: tuple[Rig, MemoryIndex],
) -> None:
    _rig, memory = graph
    _publish_trusted(memory)
    [batch] = memory.graph_display.list_batches()
    assert memory.graph_display.set_categories(
        batch.batch_id, ("entities", "relations", "evidence")
    )
    before = memory.graph_display.access()
    _publish_trusted(memory)
    assert memory.graph_display.access() == before

    memory.close()
    memory.open()
    assert memory.graph_display.access() == before
    memory.publish(
        [HistoricalMemory(HIDDEN, "未纳入快照"), HistoricalMemory(LEFT, "修改原件")],
        graph_sources=(
            MemoryGraphSource(HIDDEN, "entity", "active", name="未授权新增"),
            MemoryGraphSource(LEFT, "entity", "active", name="未授权改名"),
        ),
    )
    current = memory.graph_display.access()
    assert LEFT not in current.sources and HIDDEN not in current.sources
    assert memory.query_graph(allowed_sources=before).total == 0
    assert [node.source for node in memory.read_graph(allowed_sources=current).nodes] == [RIGHT]
    assert not memory.read_graph(allowed_sources=current).edges

    _publish_trusted(memory, include_extra=True)
    [updated] = memory.graph_display.list_batches()
    assert updated.batch_id != batch.batch_id and not updated.categories
    assert not memory.graph_display.set_categories(batch.batch_id, ("entities",))
    assert not memory.graph_display.access().sources


def test_formal_inflight_graph_cannot_survive_revoke_and_regrant(
    graph: tuple[Rig, MemoryIndex], monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, memory = graph
    set_password(rig)
    _publish_trusted(memory)
    [batch] = memory.graph_display.list_batches()
    categories = ("entities", "relations")
    assert memory.graph_display.set_categories(batch.batch_id, categories)
    ready, release = Event(), Event()
    query = memory.query_graph

    def delay(**kwargs) -> MemoryGraphPage:
        result = query(**kwargs)
        assert result.total == 1
        ready.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(memory, "query_graph", delay)
    with _client(rig, memory, formal=True) as client, ThreadPoolExecutor(1) as executor:
        client.cookies.set(SESSION_COOKIE, rig.sessions.create())
        pending = executor.submit(client.get, GRAPH, params={"neighbor_of": LEFT.encode()})
        try:
            assert ready.wait(5)
            assert _write_grants(client, batch.batch_id).status_code == 200
            assert _write_grants(client, batch.batch_id, *categories).status_code == 200
        finally:
            release.set()
        stale = pending.result(5)
        assert stale.status_code == 409 and "合成" not in stale.text
