"""验证记忆图查询只使用显式获准的合成原件。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.memory import (
    HistoricalMemory,
    MemoryGraphEvidence,
    MemoryGraphNode,
    MemoryGraphPage,
    MemoryGraphRelationDetail,
    MemoryGraphSource,
    MemoryIndex,
    MemoryKind,
    MemorySource,
)
from aureus.timeline import TimelineDatabase

NOW = datetime(2026, 11, 23, tzinfo=UTC)
LEFT = MemorySource("first", "memory", "entities", ("key-left",))
RIGHT = MemorySource("first", "memory", "entities", ({"blob": "6162"},))
THIRD = MemorySource("first", "memory", "entities", ("key-third",))
TWIN = MemorySource("second", LEFT.role, LEFT.table, LEFT.key)
HIDDEN = MemorySource("a-private", LEFT.role, LEFT.table, LEFT.key)
OUTGOING = MemorySource("first", "memory", "relations", ("key-out",))
INCOMING = MemorySource("first", "memory", "relations", ("key-in",))
LOOP = MemorySource("first", "memory", "relations", ("key-loop",))
SECOND_HOP = MemorySource("first", "memory", "relations", ("key-next",))
PARAGRAPH = MemorySource("first", "memory", "paragraphs", ("key-paragraph",))
RELATION_LINK = MemorySource("first", "memory", "paragraph_relations", ("key-paragraph", "key-out"))
ENTITY_LINK = MemorySource("first", "memory", "paragraph_entities", ("key-paragraph", "key-left"))
GRANTS = (LEFT, RIGHT, THIRD, TWIN, OUTGOING, INCOMING, LOOP, SECOND_HOP)


def _index(path: Path, clock: ManualClock) -> MemoryIndex:
    return MemoryIndex(path, TimelineDatabase(path, clock), clock)


@pytest.fixture
def index(tmp_path: Path) -> Iterator[MemoryIndex]:
    memory = _index(tmp_path / "graph.db", ManualClock(NOW))
    material = (
        MemoryGraphSource(
            LEFT,
            "entity",
            "active",
            name=" Café ",
            evidence=(MemoryGraphEvidence("证据段落", PARAGRAPH, ENTITY_LINK),),
        ),
        MemoryGraphSource(RIGHT, "entity", "active", name="Café Bar"),
        MemoryGraphSource(THIRD, "entity", "active", name="Un café"),
        MemoryGraphSource(TWIN, "entity", "active", name="Café"),
        MemoryGraphSource(HIDDEN, "entity", "active", name="CAFÉ"),
        MemoryGraphSource(
            OUTGOING,
            "relation",
            "active",
            predicate="CAFÉ",
            subject_source=LEFT,
            object_source=RIGHT,
            confidence=0.7,
            evidence=(MemoryGraphEvidence("证据段落", PARAGRAPH, RELATION_LINK),),
        ),
        MemoryGraphSource(
            INCOMING,
            "relation",
            "active",
            predicate="Café amie",
            subject_source=RIGHT,
            object_source=LEFT,
        ),
        MemoryGraphSource(
            LOOP,
            "relation",
            "active",
            predicate="Café bar",
            subject_source=LEFT,
            object_source=LEFT,
        ),
        MemoryGraphSource(
            SECOND_HOP,
            "relation",
            "active",
            predicate="Café autre",
            subject_source=RIGHT,
            object_source=THIRD,
        ),
    )
    memory.open()
    try:
        memory.publish(
            [
                *(HistoricalMemory(item.source, "原件正文") for item in material),
                HistoricalMemory(PARAGRAPH, "证据段落"),
                HistoricalMemory(RELATION_LINK, "关系关联"),
                HistoricalMemory(ENTITY_LINK, "实体关联"),
            ],
            graph_sources=material,
        )
        yield memory
    finally:
        memory.close()


def _sources(page: MemoryGraphPage) -> tuple[MemorySource, ...]:
    return tuple(
        item.source if isinstance(item, MemoryGraphNode) else item.relation.source
        for item in page.items
    )


def test_search_ranks_only_visible_matches_before_counting_and_paging(index: MemoryIndex) -> None:
    first = index.query_graph(allowed_sources=GRANTS, query="  cAFÉ ", limit=3)
    assert first.covered and first.total == 8 and first.next_offset == 3
    assert _sources(first) == (LEFT, TWIN, OUTGOING)
    assert index.graph_detail(HIDDEN, allowed_sources=GRANTS) is None
    assert index.graph_detail(HIDDEN, allowed_sources=(*GRANTS, HIDDEN)) is not None

    second = index.query_graph(allowed_sources=GRANTS[::-1], query="café", offset=3, limit=3)
    assert second.total == 8 and second.next_offset == 6
    assert _sources(second) == (RIGHT, INCOMING, SECOND_HOP)
    last = index.query_graph(allowed_sources=GRANTS, query="café", offset=6, limit=3)
    assert last.total == 8 and last.next_offset is None
    assert _sources(last) == (LOOP, THIRD)
    assert index.query_graph(allowed_sources=GRANTS, offset=8) == MemoryGraphPage(True, (), 8)

    browse = index.query_graph(allowed_sources=GRANTS, query="\t ")
    assert _sources(browse) == (LEFT, TWIN, RIGHT, THIRD, OUTGOING, INCOMING, SECOND_HOP, LOOP)
    grants = (*GRANTS, PARAGRAPH, ENTITY_LINK, RELATION_LINK)
    for query in ("key-left", "原件正文", "证据段落"):
        assert index.query_graph(allowed_sources=grants, query=query) == MemoryGraphPage(True)


def test_neighbors_keep_directions_and_use_full_source_keys(index: MemoryIndex) -> None:
    page = index.query_graph(allowed_sources=GRANTS, neighbor_of=LEFT)
    assert page.covered and page.total == 3 and page.next_offset is None
    assert _sources(page) == (OUTGOING, INCOMING, LOOP)
    assert all(isinstance(item, MemoryGraphRelationDetail) for item in page.items)
    assert [
        (item.subject.source, item.object.source)
        for item in page.items
        if isinstance(item, MemoryGraphRelationDetail)
    ] == [(LEFT, RIGHT), (RIGHT, LEFT), (LEFT, LEFT)]
    filtered = index.query_graph(allowed_sources=GRANTS, neighbor_of=LEFT, query="bar")
    assert _sources(filtered) == (LOOP,)
    assert index.query_graph(allowed_sources=GRANTS, neighbor_of=TWIN) == MemoryGraphPage(True)

    for anchor in (HIDDEN, MemorySource("first", "main", LEFT.table, LEFT.key), OUTGOING):
        assert index.query_graph(allowed_sources=GRANTS, neighbor_of=anchor) == MemoryGraphPage()
        if anchor != OUTGOING:
            assert index.graph_detail(anchor, allowed_sources=GRANTS) is None


@pytest.mark.parametrize("revoked", (OUTGOING, LEFT, RIGHT, PARAGRAPH, RELATION_LINK, ENTITY_LINK))
def test_detail_and_neighbors_respect_independent_grants(
    index: MemoryIndex, revoked: MemorySource
) -> None:
    all_grants = (*GRANTS, PARAGRAPH, ENTITY_LINK, RELATION_LINK)
    grants = tuple(source for source in all_grants if source != revoked)
    result = index.graph_detail(OUTGOING, allowed_sources=grants)
    neighborhood = index.query_graph(allowed_sources=grants, neighbor_of=LEFT)
    if revoked in (OUTGOING, LEFT, RIGHT):
        assert result is None and OUTGOING not in _sources(neighborhood)
    else:
        assert isinstance(result, MemoryGraphRelationDetail)
        assert result.relation.confidence == 0.7
        assert result.relation.evidence == (
            () if revoked in (PARAGRAPH, RELATION_LINK) else (PARAGRAPH,)
        )
        assert result.subject.evidence == (
            () if revoked in (PARAGRAPH, ENTITY_LINK) else (PARAGRAPH,)
        )
        assert result in neighborhood.items
    node = index.graph_detail(LEFT, allowed_sources=grants)
    if revoked == LEFT:
        assert node is None and neighborhood == MemoryGraphPage()
    else:
        assert isinstance(node, MemoryGraphNode) and node.source == LEFT


@pytest.mark.parametrize(("offset", "limit"), ((-1, 1), (0, 0), (0, 101)))
def test_invalid_paging_is_rejected_even_without_grants(
    tmp_path: Path, offset: int, limit: int
) -> None:
    memory = _index(tmp_path / "unopened.db", ManualClock(NOW))
    with pytest.raises(ValueError, match="offset"):
        memory.query_graph(allowed_sources=(), offset=offset, limit=limit)


def test_removal_and_restore_do_not_override_disabled_graph_items(
    index: MemoryIndex, tmp_path: Path
) -> None:
    timeline = TimelineDatabase(tmp_path / "graph.db", ManualClock(NOW))
    timeline.open()
    timeline.close()
    original = index.get_history(OUTGOING)
    assert original is not None
    relations = {
        row.source.encode(): row
        for row in index.browse_history("", "active", 0, 10, source_table="relations")
    }
    outgoing = relations[OUTGOING.encode()]
    incoming = relations[INCOMING.encode()]
    assert outgoing.entry_id is not None and incoming.entry_id is not None
    targets: list[tuple[MemoryKind, int]] = [
        ("history", outgoing.entry_id),
        ("history", incoming.entry_id),
    ]
    assert _sources(index.query_graph(allowed_sources=GRANTS, neighbor_of=LEFT)) == (
        OUTGOING,
        INCOMING,
        LOOP,
    )
    assert index.update_resource("history", incoming.entry_id, None, False)
    assert index.graph_detail(INCOMING, allowed_sources=GRANTS) is None
    assert index.set_resources_removed(targets, True)
    assert _sources(index.query_graph(allowed_sources=GRANTS, neighbor_of=LEFT)) == (LOOP,)
    assert index.graph_detail(OUTGOING, allowed_sources=GRANTS) is None
    assert index.get_history(OUTGOING) == original
    assert index.set_resources_removed(targets, False)
    assert _sources(index.query_graph(allowed_sources=GRANTS, neighbor_of=LEFT)) == (OUTGOING, LOOP)
    assert index.graph_detail(OUTGOING, allowed_sources=GRANTS) is not None
    assert index.graph_detail(INCOMING, allowed_sources=GRANTS) is None


def test_validity_boundaries_filter_graph_before_search(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    memory = _index(tmp_path / "validity.db", clock)
    step = timedelta(seconds=1)
    memory.open()
    try:
        memory.publish(
            [
                HistoricalMemory(LEFT, "锚点"),
                HistoricalMemory(RIGHT, "限时实体", valid_from=NOW + step, valid_to=NOW + 2 * step),
                HistoricalMemory(OUTGOING, "限时关系"),
            ],
            graph_sources=(
                MemoryGraphSource(LEFT, "entity", "active", name="锚点"),
                MemoryGraphSource(RIGHT, "entity", "active", name="限时实体"),
                MemoryGraphSource(
                    OUTGOING,
                    "relation",
                    "active",
                    predicate="限时关系",
                    subject_source=LEFT,
                    object_source=RIGHT,
                ),
            ),
        )
    finally:
        memory.close()

    grants = (LEFT, RIGHT, OUTGOING)
    assert memory.query_graph(allowed_sources=grants, query="限时") == MemoryGraphPage(True)
    assert memory.graph_detail(OUTGOING, allowed_sources=grants) is None
    clock.advance(step)
    assert _sources(memory.query_graph(allowed_sources=grants, query="限时")) == (RIGHT, OUTGOING)
    assert memory.graph_detail(OUTGOING, allowed_sources=grants) is not None
    clock.advance(step)
    assert memory.query_graph(allowed_sources=grants, query="限时") == MemoryGraphPage(True)
    assert memory.graph_detail(OUTGOING, allowed_sources=grants) is None


def test_readonly_query_distinguishes_legacy_and_authorized_zero_matches(
    index: MemoryIndex, tmp_path: Path
) -> None:
    index.close()
    path = tmp_path / "graph.db"
    before = path.read_bytes()
    assert index.query_graph(allowed_sources=()) == MemoryGraphPage()
    assert index.graph_detail(LEFT, allowed_sources=()) is None
    assert index.query_graph(allowed_sources=GRANTS, query="没有命中") == MemoryGraphPage(True)
    assert index.graph_detail(OUTGOING, allowed_sources=GRANTS) is not None
    assert path.read_bytes() == before

    path = tmp_path / "legacy.db"
    legacy = _index(path, ManualClock(NOW))
    legacy.open()
    try:
        legacy.publish([HistoricalMemory(LEFT, "仅文字的实体原件")])
    finally:
        legacy.close()
    before = path.read_bytes()
    assert legacy.query_graph(allowed_sources=(LEFT,), limit=100) == MemoryGraphPage()
    assert legacy.graph_detail(LEFT, allowed_sources=(LEFT,)) is None
    assert path.read_bytes() == before

    path = tmp_path / "missing.db"
    missing = _index(path, ManualClock(NOW))
    assert missing.query_graph(allowed_sources=()) == MemoryGraphPage()
    with pytest.raises(sqlite3.OperationalError):
        missing.query_graph(allowed_sources=(LEFT,))
    assert not path.exists()
