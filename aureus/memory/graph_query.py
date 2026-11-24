"""在已授权的记忆图投影上查询，不读取或补充原件。"""

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import chain

from aureus.memory.graph import MemoryGraph, MemoryGraphNode, MemoryGraphRelation
from aureus.memory.history import MemorySource

MAX_GRAPH_PAGE_SIZE = 100


@dataclass(frozen=True)
class MemoryGraphRelationDetail:
    """一条获准关系及其有向两端；证据仍限于已获准的段落。"""

    relation: MemoryGraphRelation
    subject: MemoryGraphNode
    object: MemoryGraphNode


type MemoryGraphItem = MemoryGraphNode | MemoryGraphRelationDetail


@dataclass(frozen=True)
class MemoryGraphPage:
    """当前授权覆盖、匹配项的总数和下一页位置。"""

    covered: bool = False
    items: tuple[MemoryGraphItem, ...] = ()
    total: int = 0
    next_offset: int | None = None


def _relations(
    graph: MemoryGraph, nodes: dict[str, MemoryGraphNode], anchor: str | None = None
) -> Iterator[MemoryGraphRelationDetail]:
    for edge in graph.edges:
        subject, target = edge.subject.encode(), edge.object.encode()
        if anchor is None or anchor in (subject, target):
            for relation in edge.relations:
                yield MemoryGraphRelationDetail(relation, nodes[subject], nodes[target])


def _match_rank(text: str, query: str) -> int | None:
    if not query or text == query:
        return 0
    if text.startswith(query):
        return 1
    return 2 if query in text else None


def query_graph(
    graph: MemoryGraph, *, query: str, neighbor_of: MemorySource | None, offset: int, limit: int
) -> MemoryGraphPage:
    """在可见投影中按名称或谓词排序，计数后分页。"""
    nodes = {node.source.encode(): node for node in graph.nodes}
    anchor = neighbor_of.encode() if neighbor_of is not None else None
    if anchor is not None and anchor not in nodes:
        return MemoryGraphPage()

    relations = _relations(graph, nodes, anchor)
    items = chain(graph.nodes if anchor is None else (), relations)
    normalized_query = query.strip().casefold()
    matches: list[tuple[tuple[int, bool, str, str], MemoryGraphItem]] = []
    for item in items:
        is_relation = isinstance(item, MemoryGraphRelationDetail)
        if is_relation:
            text, source = item.relation.predicate, item.relation.source
        else:
            text, source = item.name, item.source
        text = text.strip().casefold()
        rank = _match_rank(text, normalized_query)
        if rank is not None:
            matches.append(((rank, is_relation, text, source.encode()), item))
    matches.sort(key=lambda match: match[0])
    end = offset + limit
    return MemoryGraphPage(
        graph.covered,
        tuple(item for _, item in matches[offset:end]),
        len(matches),
        end if end < len(matches) else None,
    )


def graph_detail(graph: MemoryGraph, source: MemorySource) -> MemoryGraphItem | None:
    """按完整来源查找可见详情；无权或不存在时均不返回资料。"""
    nodes = {node.source.encode(): node for node in graph.nodes}
    key = source.encode()
    if key in nodes:
        return nodes[key]
    return next(
        (item for item in _relations(graph, nodes) if item.relation.source.encode() == key), None
    )
