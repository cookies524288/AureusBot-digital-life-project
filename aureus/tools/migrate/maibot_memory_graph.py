"""按固定公开版本的独立字段保存图来源，不从正文猜测端点。"""

import math
from collections import defaultdict
from collections.abc import Iterable, Iterator

from aureus.memory import MemoryGraphEvidence, MemoryGraphSource, MemorySource, MemoryStatus
from aureus.tools.migrate.maibot_memory_rows import StagedRow


def _string(row: StagedRow, field: str) -> str:
    value = row.values.get(field)
    return value if isinstance(value, str) else ""


def graph_sources(
    memory: dict[str, dict[str, StagedRow]],
    links: Iterable[StagedRow],
    status: dict[str, MemoryStatus],
) -> Iterator[MemoryGraphSource]:
    """同实例原始名称规范化后唯一才解析；歧义与缺件保留为未解析。"""
    names: dict[tuple[str, str, str], list[MemorySource]] = defaultdict(list)
    evidence: dict[str, list[MemoryGraphEvidence]] = defaultdict(list)
    entities = memory.get("entities", {})
    paragraphs = memory.get("paragraphs", {})
    for row in entities.values():
        name = _string(row, "name").strip().lower()
        if name:
            names[row.source.instance, row.source.role, name].append(row.source)
    for link in links:
        entity = link.source.table == "paragraph_entities"
        table, field = ("entities", "entity_hash") if entity else ("relations", "relation_hash")
        owner = memory.get(table, {}).get(_string(link, field))
        if owner is None:
            continue
        reference = _string(link, "paragraph_hash")
        paragraph = paragraphs.get(reference)
        evidence[owner.source.encode()].append(
            MemoryGraphEvidence(reference, paragraph.source if paragraph else None, link.source)
        )
    for table in ("entities", "relations", "deleted_relations"):
        for row in memory.get(table, {}).values():
            key = row.source.encode()
            if table == "entities":
                yield MemoryGraphSource(
                    row.source,
                    "entity",
                    status[key],
                    name=_string(row, "name"),
                    evidence=tuple(evidence[key]),
                )
                continue
            subject, predicate, target = (
                _string(row, field) for field in ("subject", "predicate", "object")
            )
            endpoints: list[MemorySource | None] = []
            for name in (subject, target):
                candidates = names.get(
                    (row.source.instance, row.source.role, name.strip().lower()), []
                )
                endpoints.append(candidates[0] if len(candidates) == 1 else None)
            if reference := _string(row, "source_paragraph"):
                paragraph = paragraphs.get(reference)
                evidence[key].append(
                    MemoryGraphEvidence(reference, paragraph.source if paragraph else None)
                )
            confidence = row.values.get("confidence")
            numeric_confidence = (
                float(confidence)
                if isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and math.isfinite(confidence)
                else None
            )
            yield MemoryGraphSource(
                row.source,
                "relation",
                status[key],
                subject=subject,
                predicate=predicate,
                object=target,
                subject_source=endpoints[0],
                object_source=endpoints[1],
                confidence=numeric_confidence,
                evidence=tuple(evidence[key]),
            )
