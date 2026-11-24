"""记忆图的结构化来源和显式授权范围内的只读投影。"""

import json
import sqlite3
from collections.abc import Collection, Iterable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from aureus.common.sqlite import SQLITE_BUSY_TIMEOUT_SECONDS
from aureus.memory.history import MemorySource, MemoryStatus
from aureus.memory.message_boundary import is_message_source


@dataclass(frozen=True)
class MemoryGraphEvidence:
    """原始段落引用；关联行与目标段落分别授权，未解析引用仅存档。"""

    reference: str
    source: MemorySource | None
    link_source: MemorySource | None = None


@dataclass(frozen=True)
class MemoryGraphSource:
    """来自独立原字段的图材料；未解析端点不替换为显示名称。"""

    source: MemorySource
    kind: Literal["entity", "relation"]
    status: MemoryStatus
    name: str = ""
    subject: str = ""
    predicate: str = ""
    object: str = ""
    subject_source: MemorySource | None = None
    object_source: MemorySource | None = None
    confidence: float | None = None
    evidence: tuple[MemoryGraphEvidence, ...] = ()


@dataclass(frozen=True)
class MemoryGraphNode:
    """获准实体及其获准段落证据，不代表一个账号或人物档案。"""

    source: MemorySource
    name: str
    evidence: tuple[MemorySource, ...]


@dataclass(frozen=True)
class MemoryGraphRelation:
    """同一有向边上的一条关系，置信度与证据数量分别保留。"""

    source: MemorySource
    predicate: str
    confidence: float | None
    evidence: tuple[MemorySource, ...]


@dataclass(frozen=True)
class MemoryGraphEdge:
    """按完整端点来源聚合的有向边，不自动生成反向边。"""

    subject: MemorySource
    object: MemorySource
    relations: tuple[MemoryGraphRelation, ...]


@dataclass(frozen=True)
class MemoryGraph:
    """仅反映获准材料的覆盖与结果，无覆盖不代表全库没有关系。"""

    covered: bool = False
    nodes: tuple[MemoryGraphNode, ...] = ()
    edges: tuple[MemoryGraphEdge, ...] = ()


def put_graph_sources(connection: sqlite3.Connection, sources: Iterable[MemoryGraphSource]) -> None:
    """在原件发布事务内保存结构化材料；无材料时不创建或回填表。"""
    initialized = False
    for item in sources:
        if not initialized:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS memory_graph_sources (
                    source_key TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    name TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,
                    object TEXT NOT NULL, subject_source TEXT, object_source TEXT,
                    confidence REAL, evidence TEXT NOT NULL
                )
            """)
            initialized = True
        connection.execute(
            """INSERT INTO memory_graph_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_key) DO UPDATE SET
                   (kind, status, name, subject, predicate, object,
                    subject_source, object_source, confidence, evidence) =
                   (excluded.kind, excluded.status, excluded.name, excluded.subject,
                    excluded.predicate, excluded.object, excluded.subject_source,
                    excluded.object_source, excluded.confidence, excluded.evidence)
               WHERE (kind, status, name, subject, predicate, object,
                      subject_source, object_source, confidence, evidence) IS NOT
                     (excluded.kind, excluded.status, excluded.name, excluded.subject,
                      excluded.predicate, excluded.object, excluded.subject_source,
                      excluded.object_source, excluded.confidence, excluded.evidence)""",
            (
                item.source.encode(),
                item.kind,
                item.status,
                item.name,
                item.subject,
                item.predicate,
                item.object,
                item.subject_source.encode() if item.subject_source else None,
                item.object_source.encode() if item.object_source else None,
                item.confidence,
                json.dumps([asdict(link) for link in item.evidence], ensure_ascii=False),
            ),
        )


def _source(value: dict[str, object]) -> MemorySource:
    return MemorySource.decode(
        json.dumps([value["instance"], value["role"], value["table"], value["key"]])
    )


def _same_scope(owner: MemorySource, target: MemorySource, table: str) -> bool:
    return owner.instance == target.instance and owner.role == target.role and target.table == table


def _evidence(
    row: sqlite3.Row, allowed: dict[str, sqlite3.Row], owner: MemorySource
) -> tuple[MemorySource, ...]:
    visible: dict[str, MemorySource] = {}
    for item in json.loads(row["evidence"]):
        if item["source"] is None:
            continue
        target = _source(item["source"])
        if target.encode() not in allowed or not _same_scope(owner, target, "paragraphs"):
            continue
        if item["link_source"] is not None:
            link = _source(item["link_source"])
            table = "paragraph_entities" if row["kind"] == "entity" else "paragraph_relations"
            if link.encode() not in allowed or not _same_scope(owner, link, table):
                continue
        visible[target.encode()] = target
    return tuple(visible[key] for key in sorted(visible))


def project_graph(rows: list[sqlite3.Row]) -> MemoryGraph:
    """将调用方已经筛选的原件投影为有向关系与可用证据。"""
    allowed = {row["source_key"]: row for row in rows}
    nodes: dict[str, MemoryGraphNode] = {}
    for key, row in allowed.items():
        if row["kind"] == "entity" and row["graph_status"] == "active" and row["name"].strip():
            source = MemorySource.decode(key)
            nodes[key] = MemoryGraphNode(source, row["name"], _evidence(row, allowed, source))
    edges: dict[tuple[str, str], list[MemoryGraphRelation]] = {}
    for key, row in allowed.items():
        if row["kind"] != "relation" or row["graph_status"] != "active":
            continue
        subject, target = row["subject_source"], row["object_source"]
        if subject not in nodes or target not in nodes or not row["predicate"].strip():
            continue
        source = MemorySource.decode(key)
        if not all(
            _same_scope(source, nodes[endpoint].source, "entities")
            for endpoint in (subject, target)
        ):
            continue
        edges.setdefault((subject, target), []).append(
            MemoryGraphRelation(
                source, row["predicate"], row["confidence"], _evidence(row, allowed, source)
            )
        )
    return MemoryGraph(
        covered=bool(nodes),
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=tuple(
            MemoryGraphEdge(
                nodes[subject].source, nodes[target].source, tuple(edges[subject, target])
            )
            for subject, target in sorted(edges)
        ),
    )


def read_graph(
    database: Path, *, allowed_sources: Collection[MemorySource], now: datetime
) -> MemoryGraph:
    """只读已获准且有效的原件；空授权和旧库无材料均返回无覆盖。

    调用者只可传入已独立获准的原件；端点可见或知晓来源键均不是关系授权。
    未解析端点不返回，段落证据另按目标及关联行过滤。库不可读时抛 SQLite 错误。
    """
    if not allowed_sources:
        return MemoryGraph()
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        if not has_graph_sources(connection):
            return MemoryGraph()
        rows = connection.execute(
            f"""SELECT history.source_key, graph.kind, graph.status AS graph_status, graph.name,
                      graph.subject_source, graph.object_source, graph.predicate,
                      graph.confidence, graph.evidence
               FROM memory_history history
               LEFT JOIN memory_graph_sources graph ON graph.source_key = history.source_key
               LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = history.id
               WHERE history.source_key IN (SELECT value FROM json_each(?))
                 AND NOT ({is_message_source("history.source_key")})
                 AND (history.status = 'active' OR (history.status = 'missing' AND (
                     graph.kind = 'entity' OR json_extract(history.source_key, '$[2]') IN
                         ('paragraph_entities', 'paragraph_relations'))))
                 AND (history.valid_from IS NULL OR history.valid_from <= ?)
                 AND (history.valid_to IS NULL OR history.valid_to > ?)
                 AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)
                 AND NOT coalesce(edit.is_bundle_removed, 0)
               ORDER BY history.source_key""",
            (
                json.dumps([source.encode() for source in allowed_sources]),
                now.timestamp(),
                now.timestamp(),
            ),
        ).fetchall()
        return project_graph(rows)


def has_graph_sources(connection: sqlite3.Connection) -> bool:
    """不初始化旧库，仅识别是否已经保留结构化原件。"""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'memory_graph_sources' AND type = 'table'"
    ).fetchone()
    return row is not None
