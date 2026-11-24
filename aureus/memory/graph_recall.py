"""适配 A_Memorix 2.0.0 的实体图关系召回（AGPL-3.0）。

来源：https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/retrieval/graph_relation_recall.py
保留一实体的入边/出边、两实体的直连/有限两跳，以及原关系方向。
以 Aureus 的迁移快照与维护状态约束材料，不复用上游存储、模型或向量调用。
"""

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime

from aureus.memory.graph import MemoryGraph, has_graph_sources, project_graph
from aureus.memory.graph_query import MemoryGraphRelationDetail
from aureus.memory.history import HistoricalMemory, MemoryEvidence

_MAX_GRAPH_SEEDS = 2
_MIN_DIRECT_RELATIONS = 3
_MAX_PAIR_PATHS = 4


def _seed_names(graph: MemoryGraph, keywords: Sequence[str]) -> list[str]:
    words = tuple(word.strip().lower() for word in keywords if word.strip())
    names = {node.name.strip().lower() for node in graph.nodes}
    matches = [name for name in names if any(name in word for word in words)]
    ranked = sorted(matches, key=lambda name: (name not in words, -len(name), name))
    return ranked[:_MAX_GRAPH_SEEDS]


def _collect_edges(graph: MemoryGraph, seeds: Sequence[str]) -> Iterable[MemoryGraphRelationDetail]:
    nodes = {node.source.encode(): node for node in graph.nodes}
    neighbors: dict[str, dict[str, list[MemoryGraphRelationDetail]]] = defaultdict(dict)
    scopes: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for key, node in nodes.items():
        if (name := node.name.strip().lower()) in seeds:
            scopes[node.source.instance, node.source.role][name].append(key)
    for edge in graph.edges:
        subject, target = edge.subject.encode(), edge.object.encode()
        pair = neighbors[subject].setdefault(target, [])
        neighbors[target][subject] = pair
        pair.extend(
            MemoryGraphRelationDetail(relation, nodes[subject], nodes[target])
            for relation in edge.relations
        )
    for names in scopes.values():
        if any(len(matches) != 1 for matches in names.values()):
            continue
        anchors = [names[seed][0] for seed in seeds if seed in names]
        first = anchors[0]
        if len(anchors) == 1:
            for relations in neighbors[first].values():
                yield from relations
            continue
        second = anchors[1]
        direct = neighbors[first].get(second, [])
        yield from direct
        if len(direct) < _MIN_DIRECT_RELATIONS:
            middle_nodes = (neighbors[first].keys() & neighbors[second].keys()) - {first, second}
            for middle in sorted(middle_nodes)[:_MAX_PAIR_PATHS]:
                yield from neighbors[first][middle]
                yield from neighbors[middle][second]


def search_graph_relations(
    connection: sqlite3.Connection,
    keywords: Sequence[str],
    limit: int,
    *,
    now: datetime,
    max_chars: int,
) -> list[HistoricalMemory]:
    """仅从已发布迁移原件生成实际回复参考；不产生网页授权。"""
    if not keywords or not has_graph_sources(connection):
        return []
    point = now.timestamp()
    rows = connection.execute(
        """SELECT history.source_key, graph.kind, graph.status AS graph_status, graph.name,
                  graph.subject_source, graph.object_source, graph.predicate,
                  graph.confidence, graph.evidence,
                  coalesce(edit.edited_content, history.content) AS content
           FROM memory_history history
           JOIN memory_history_snapshots snapshot
                ON snapshot.instance = json_extract(history.source_key, '$[0]')
           LEFT JOIN memory_graph_sources graph ON graph.source_key = history.source_key
           LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = history.id
           WHERE json_extract(history.source_key, '$[1]') = 'memory'
             AND json_extract(history.source_key, '$[2]') IN
                 ('entities', 'relations', 'paragraphs',
                  'paragraph_entities', 'paragraph_relations')
             AND (history.status = 'active' OR (history.status = 'missing' AND (
                 graph.kind = 'entity' OR json_extract(history.source_key, '$[2]') IN
                     ('paragraph_entities', 'paragraph_relations'))))
             AND (history.valid_from IS NULL OR history.valid_from <= ?)
             AND (history.valid_to IS NULL OR history.valid_to > ?)
             AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)
             AND (graph.kind IS NULL OR edit.edited_content IS NULL)
           ORDER BY history.source_key""",
        (point, point),
    ).fetchall()
    graph = project_graph(rows)
    seeds = _seed_names(graph, keywords)
    if not seeds:
        return []
    texts = {row["source_key"]: row["content"] for row in rows}
    selected: dict[str, HistoricalMemory] = {}
    for detail in _collect_edges(graph, seeds):
        relation = detail.relation
        key = relation.source.encode()
        if key in selected:
            continue
        text = f"{detail.subject.name} —[{relation.predicate}]→ {detail.object.name}"[:max_chars]
        evidence: tuple[MemoryEvidence, ...] = ()
        marker = "；证据："
        remaining = max_chars - len(text) - len(marker)
        if remaining > 0:
            for paragraph in relation.evidence:
                if excerpt := texts[paragraph.encode()].strip()[:remaining]:
                    text += marker + excerpt
                    evidence = (MemoryEvidence("paragraph", paragraph.encode(), paragraph),)
                    break
        selected[key] = HistoricalMemory(relation.source, text, "关系图", evidence=evidence)
        if len(selected) >= limit:
            break
    return list(selected.values())
