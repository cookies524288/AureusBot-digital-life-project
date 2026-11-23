"""用合成暂存行验证图来源不从文字、证据共现或授权子集反推。"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from aureus.cli.main import main
from aureus.common import ManualClock
from aureus.memory import (
    HistoricalMemory,
    MemoryGraph,
    MemoryGraphSource,
    MemoryIndex,
    MemorySource,
)
from aureus.timeline import TimelineDatabase

NOW = datetime(2026, 11, 23, tzinfo=UTC)


def _source(table: str, *key: str, instance: str = "first") -> MemorySource:
    return MemorySource(instance, "memory", table, key)


def _make_sources(root: Path) -> tuple[Path, Path]:
    main_db, memory_db = root / "main.db", root / "memory.db"
    with closing(sqlite3.connect(main_db)) as database:
        database.execute("CREATE TABLE mai_messages (id INTEGER PRIMARY KEY)")
    with closing(sqlite3.connect(memory_db)) as database:
        database.executescript("""
            CREATE TABLE entities (hash TEXT PRIMARY KEY, name TEXT UNIQUE, is_deleted INTEGER);
            INSERT INTO entities VALUES
                ('a', '甲', 0), ('bc', '乙 丙', 0), ('c', '丙', 0), ('deleted', '删', 1),
                ('amb1', 'Apple', 0), ('amb2', ' apple ', 0);
            CREATE TABLE paragraphs (hash TEXT PRIMARY KEY, content TEXT, is_deleted INTEGER);
            INSERT INTO paragraphs VALUES
                ('first', '已授权段落', 0), ('linked', '另一个证据', 0),
                ('hidden', '隐藏段落', 0), ('deleted', '删除段落', 1);
            CREATE TABLE relations (
                hash TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
                confidence REAL, source_paragraph TEXT, is_inactive INTEGER
            );
            INSERT INTO relations VALUES
                ('r1', '甲', '认识', '乙 丙', 0.4, 'first', 0),
                ('r2', '甲', '认识 乙', '丙', 0.6, NULL, 0),
                ('r3', '甲', '帮助', '乙 丙', NULL, 'missing', 0),
                ('back', '乙 丙', '来访', '甲', 0.9, NULL, 0),
                ('amb-subject', 'APPLE', '帮助', '甲', 0.5, NULL, 0),
                ('amb-object', '甲', '帮助', 'APPLE', 0.5, NULL, 0),
                ('missing', '甲', '寻找', '未建实体', 1, NULL, 0),
                ('hidden', '丙', '不公开', '甲', 1, 'hidden', 0),
                ('deleted', '甲', '经过', '删', 1, NULL, 0),
                ('inactive', '丙', '失活', '甲', 1, NULL, 1);
            CREATE TABLE deleted_relations (
                hash TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
                confidence REAL, source_paragraph TEXT, is_inactive INTEGER
            );
            INSERT INTO deleted_relations VALUES ('recycled', '丙', '已删除', '甲', 1, NULL, 0);
            CREATE TABLE paragraph_relations (
                paragraph_hash TEXT, relation_hash TEXT, PRIMARY KEY (paragraph_hash, relation_hash)
            );
            INSERT INTO paragraph_relations VALUES
                ('first', 'r1'), ('linked', 'r1'), ('hidden', 'r1'), ('deleted', 'r1'),
                ('missing', 'r1'), ('hidden', 'hidden');
            CREATE TABLE paragraph_entities (
                paragraph_hash TEXT, entity_hash TEXT, PRIMARY KEY (paragraph_hash, entity_hash)
            );
            INSERT INTO paragraph_entities VALUES ('first', 'a'), ('first', 'amb1');
        """)
    return main_db, memory_db


def _publish(root: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    stage, home = root / "stage.db", root / "target"
    sources = _make_sources(root)
    for instance in ("first", "second"):
        for role, source in zip(("main", "memory"), sources, strict=True):
            package = root / f"{instance}-{role}.zip"
            options = ["--instance", instance, "--role", role]
            assert main(["migrate-maibot", "export", *options, str(source), str(package)]) == 0
            assert main(["migrate-maibot", "import", *options, str(package), str(stage)]) == 0
        command = [
            "migrate-maibot",
            "publish-memory",
            str(stage),
            "--instance",
            instance,
            "--source-timezone",
            "UTC",
            "--target-home",
            str(home),
        ]
        capsys.readouterr()
        assert main(command) == 0
        report = capsys.readouterr().out
        assert main(command) == 0
        assert capsys.readouterr().out == report
    return home / "data" / "aureus.db"


def _index(path: Path) -> MemoryIndex:
    clock = ManualClock(NOW)
    return MemoryIndex(path, TimelineDatabase(path, clock), clock)


def test_graph_sources_keep_original_direction_and_refuse_ambiguous_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _publish(tmp_path, capsys)
    index = _index(path)
    index.open()
    first = index.get_history(_source("relations", "r1"))
    second = index.get_history(_source("relations", "r2"))
    assert first is not None and second is not None and first.text == second.text
    assert {first.source, second.source} <= {
        hit.source for hit in index.search([first.text], (), 10)
    }
    index.close()
    index.open()
    index.close()

    grants = [
        *(_source("entities", key) for key in ("a", "bc", "c", "amb1", "deleted")),
        *(_source("entities", key, instance="second") for key in ("a", "bc")),
        *(
            _source("relations", key)
            for key in (
                "r1",
                "r2",
                "r3",
                "amb-subject",
                "amb-object",
                "missing",
                "deleted",
                "inactive",
            )
        ),
        _source("relations", "r1", instance="second"),
        _source("deleted_relations", "recycled"),
        _source("paragraphs", "first"),
        _source("paragraph_entities", "first", "amb1"),
    ]
    graph = index.read_graph(allowed_sources=grants)
    assert graph.covered and len(graph.nodes) == 6
    assert sum(node.name == "甲" for node in graph.nodes) == 2
    by_edge = {
        (edge.subject.encode(), edge.object.encode()): edge.relations for edge in graph.edges
    }
    assert len(by_edge) == 3
    start = _source("entities", "a").encode()
    target = _source("entities", "bc").encode()
    assert [(item.predicate, item.confidence) for item in by_edge[start, target]] == [
        ("认识", 0.4),
        ("帮助", None),
    ]
    assert (target, start) not in by_edge
    reverse = index.read_graph(allowed_sources=[*grants, _source("relations", "back")])
    assert len(reverse.edges) == 4
    assert any(
        edge.subject.encode() == target and edge.object.encode() == start for edge in reverse.edges
    )

    with closing(sqlite3.connect(path)) as database:
        rows = database.execute("""
            SELECT source_key, subject, predicate, object, subject_source, object_source, evidence
            FROM memory_graph_sources WHERE kind = 'relation' ORDER BY source_key
        """).fetchall()
    material = {row[0]: row[1:] for row in rows}
    assert len(material) == 22
    assert material[first.source.encode()][:3] == ("甲", "认识", "乙 丙")
    assert material[second.source.encode()][:3] == ("甲", "认识 乙", "丙")
    assert material[_source("relations", "amb-subject").encode()][3:5] == (None, start)
    assert material[_source("relations", "amb-object").encode()][3:5] == (start, None)
    assert material[_source("relations", "missing").encode()][3:5] == (start, None)
    assert any(
        item["source"] is None and item["reference"] == "missing"
        for item in json.loads(material[first.source.encode()][5])
    )


def test_graph_reads_only_independently_granted_originals_and_visible_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _publish(tmp_path, capsys)
    index = _index(path)
    grants = [_source("entities", key) for key in ("a", "bc", "c")]
    first, private = (_source("relations", key) for key in ("r1", "hidden"))
    foreign = _source("paragraphs", "hidden", instance="second")
    hidden_link = _source("paragraph_relations", "hidden", "hidden")
    assert index.read_graph(allowed_sources=[]) == MemoryGraph()
    assert not index.read_graph(allowed_sources=[*grants, foreign, hidden_link]).edges
    assert index.read_graph(allowed_sources=[first]) == MemoryGraph()
    graph = index.read_graph(allowed_sources=[*grants, first, private, foreign, hidden_link])
    assert len(graph.edges) == 2
    assert all(not relation.evidence for edge in graph.edges for relation in edge.relations)

    paragraph = _source("paragraphs", "first")
    linked = _source("paragraphs", "linked")
    deleted = _source("paragraphs", "deleted")
    entity_link = _source("paragraph_entities", "first", "a")
    grants += [
        first,
        paragraph,
        linked,
        deleted,
        entity_link,
        _source("paragraph_relations", "first", "r1"),
        _source("paragraph_relations", "deleted", "r1"),
        _source("paragraph_relations", "missing", "r1"),
    ]
    graph = index.read_graph(allowed_sources=grants)
    assert len(graph.edges) == 1 and graph.edges[0].relations[0].evidence == (paragraph,)
    assert graph.nodes[0].evidence == (paragraph,)
    grants.append(_source("paragraph_relations", "linked", "r1"))
    graph = index.read_graph(allowed_sources=grants)
    assert graph.edges[0].relations[0].evidence == (paragraph, linked)

    for source, column in ((linked, "is_enabled"), (first, "is_removed")):
        index.open()
        rows = index.browse_history("", "", 0, 25, source_table=source.table)
        entry = next(row.entry_id for row in rows if row.source == source)
        assert entry is not None
        with closing(sqlite3.connect(path)) as database, database:
            database.execute(
                f"INSERT INTO memory_edits (kind, entry_id, {column}) VALUES ('history', ?, ?)",
                (entry, int(column == "is_removed")),
            )
        index.close()
        graph = index.read_graph(allowed_sources=grants)
        if column == "is_enabled":
            assert graph.edges[0].relations[0].evidence == (paragraph,)
        else:
            assert not graph.edges


def test_graph_query_is_readonly_and_text_archive_has_no_graph_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _publish(tmp_path, capsys)
    index = _index(path)
    grants = [_source("entities", "a"), _source("entities", "bc"), _source("relations", "r1")]
    before = path.read_bytes()
    assert index.read_graph(allowed_sources=grants).edges
    assert path.read_bytes() == before

    archive = BytesIO()
    index.export_archive(archive)
    items = [json.loads(line) for line in archive.getvalue().splitlines()]
    assert items[0] == {"format": "aureus-memory", "version": 1}
    assert all("subject_source" not in item and "graph_sources" not in item for item in items)
    old_path = tmp_path / "text-only.db"
    legacy = _index(old_path)
    legacy.open()
    archive.seek(0)
    added, skipped = legacy.import_archive(archive)
    assert added > 0 and skipped == 0
    assert legacy.browse_history("认识", "active", 0, 10, source_table="relations")
    assert legacy.search(["认识"], (), 10)
    legacy.close()
    before = old_path.read_bytes()
    assert legacy.read_graph(allowed_sources=grants) == MemoryGraph()
    assert old_path.read_bytes() == before
    with closing(sqlite3.connect(old_path)) as database:
        assert (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memory_graph_sources'"
            ).fetchone()
            is None
        )


def test_graph_publication_rolls_back_original_and_snapshot_on_failure(tmp_path: Path) -> None:
    index = _index(tmp_path / "atomic.db")
    index.open()
    source = _source("entities", "atomic")
    material = MemoryGraphSource(source, "entity", "active", name="合成实体")

    def broken_graph() -> Iterator[MemoryGraphSource]:
        yield material
        raise ValueError("合成发布失败")

    original = HistoricalMemory(source, "合成原件")
    with pytest.raises(ValueError, match="合成发布失败"):
        index.publish([original], snapshot=("first", "broken"), graph_sources=broken_graph())
    assert index.get_history(source) is None
    assert index.read_graph(allowed_sources=[source]) == MemoryGraph()
    assert index.publish([original], snapshot=("first", "valid"), graph_sources=[material]) == 1
    assert len(index.read_graph(allowed_sources=[source]).nodes) == 1
    index.close()
