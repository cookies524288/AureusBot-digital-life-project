"""把兼容的 A_Memorix 包成员写入 Aureus 原件和图谱投影。"""

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from aureus.memory.bundle_facts import project_facts
from aureus.memory.bundle_format import (
    MemoryBundle,
    MemoryBundleError,
    bundle_digest,
    paragraph_hash,
)
from aureus.memory.graph import MemoryGraphEvidence, MemoryGraphSource
from aureus.memory.history import HistoricalMemory, MemoryEvidence, MemorySource, MemoryStatus

_FULL_KEYS = {
    "paragraphs": ("hash",),
    "entities": ("hash",),
    "relations": ("hash",),
    "paragraph_entities": ("paragraph_hash", "entity_hash"),
    "paragraph_relations": ("paragraph_hash", "relation_hash"),
    "episodes": ("episode_id",),
    "episode_paragraphs": ("episode_id", "paragraph_hash"),
    "external_memory_refs": ("external_id",),
    "fact_claims": ("claim_id",),
    "fact_evidence": ("claim_id", "evidence_type", "evidence_id", "stance"),
    "fact_transitions": ("transition_id",),
    "person_profile_snapshots": ("snapshot_id",),
    "person_profile_overrides": ("person_id",),
    "person_profile_alias_overrides": ("person_id",),
}

PROFILE_TABLES = tuple(table for table in _FULL_KEYS if table.startswith("person_profile_"))


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise MemoryBundleError(f"记忆包项目 {label} 缺少有效文字。")
    return value


def _rows(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise MemoryBundleError(f"记忆包项目 {label} 不是对象数组。")
    return value


def _stamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise MemoryBundleError("记忆包含无效时间。")
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, ValueError, OSError) as error:
        raise MemoryBundleError("记忆包含无效时间。") from error


def _source(table: str, *key: str, scope: str = "shared") -> MemorySource:
    return MemorySource("a-memorix-bundle", scope, table, key)


@dataclass
class BundleProjection:
    """原件、图投影及两者的证据关系。"""

    records: dict[MemorySource, HistoricalMemory]
    graph: dict[MemorySource, MemoryGraphSource]
    profiles: dict[str, list[dict[str, Any]]] | None = None

    def link(self, table: str, paragraph: MemorySource, target: MemorySource) -> None:
        """建立受独立归属约束的关联证据。"""
        source = _source(table, str(paragraph.key[0]), str(target.key[0]), scope=target.role)
        self.records[source] = HistoricalMemory(source, "", status="missing")
        item = self.graph[target]
        evidence = MemoryGraphEvidence(str(paragraph.key[0]), paragraph, source)
        if evidence not in item.evidence:
            self.graph[target] = replace(item, evidence=(*item.evidence, evidence))

    def entity(self, source: MemorySource, name: str, *, status: MemoryStatus = "active") -> None:
        """保存实体，不将实体名称用作一条独立的文字召回记录。"""
        self.records.setdefault(source, HistoricalMemory(source, name, status="missing"))
        self.graph.setdefault(source, MemoryGraphSource(source, "entity", status, name=name))

    def relation(
        self,
        source: MemorySource,
        triple: tuple[str, str, str],
        endpoints: tuple[MemorySource, MemorySource],
        *,
        status: MemoryStatus = "active",
        confidence: float | None = None,
    ) -> None:
        """关系同时进入文字索引和独立授权的图投影。"""
        self.records.setdefault(source, HistoricalMemory(source, " · ".join(triple), status=status))
        self.graph.setdefault(
            source,
            MemoryGraphSource(
                source,
                "relation",
                status,
                subject=triple[0],
                predicate=triple[1],
                object=triple[2],
                subject_source=endpoints[0],
                object_source=endpoints[1],
                confidence=confidence,
            ),
        )


def _paragraph(
    source: MemorySource, text: str, fields: dict[str, Any], *, status: MemoryStatus = "active"
) -> HistoricalMemory:
    stamp = next(
        (
            fields[key]
            for key in ("event_time", "event_time_start", "created_at")
            if fields.get(key) is not None
        ),
        None,
    )
    upstream = fields.get("source")
    metadata = fields.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, RecursionError) as error:
            raise MemoryBundleError("记忆包来源元数据无法解析。") from error
    conversation = metadata.get("chat_id") if isinstance(metadata, dict) else None
    evidence = tuple(
        MemoryEvidence(kind, label)
        for kind, label in (("maibot_source", upstream), ("maibot_conversation", conversation))
        if isinstance(label, str) and label
    )
    precision = fields.get("time_granularity")
    return HistoricalMemory(
        source,
        text,
        recorded_at=_stamp(stamp),
        status=status,
        time_precision=precision if isinstance(precision, str) else None,
        valid_to=_stamp(fields.get("expires_at")),
        evidence=evidence,
    )


def _knowledge_docs(bundle: MemoryBundle) -> BundleProjection:
    projection = BundleProjection({}, {})
    for doc in _rows(bundle.knowledge.get("docs"), "docs"):
        passage = _text(doc.get("passage"), "passage")
        paragraph = _source("paragraphs", paragraph_hash(passage))
        extension = doc.get("a_memorix", {})
        if not isinstance(extension, dict) or not isinstance(extension.get("time_meta", {}), dict):
            raise MemoryBundleError("段落 a_memorix 或 time_meta 格式无效。")
        projection.records.setdefault(
            paragraph,
            _paragraph(
                paragraph,
                passage.strip(),
                extension.get("time_meta", {})
                | {
                    "source": extension.get("source"),
                    "metadata": extension.get("metadata"),
                },
            ),
        )
        entities = doc.get("extracted_entities", [])
        triples = doc.get("extracted_triples", [])
        if not isinstance(entities, list) or not isinstance(triples, list):
            raise MemoryBundleError("段落实体或三元组格式无效。")

        def entity(name: str, paragraph: MemorySource = paragraph) -> MemorySource:
            source = _source("entities", name.strip().lower())
            projection.entity(source, name)
            projection.link("paragraph_entities", paragraph, source)
            return source

        for name in entities:
            entity(_text(name, "entity").strip())
        for item in triples:
            if not isinstance(item, list) or len(item) != 3:
                raise MemoryBundleError("段落三元组格式无效。")
            triple = (
                _text(item[0], "subject"),
                _text(item[1], "predicate"),
                _text(item[2], "object"),
            )
            relation = _source("relations", bundle_digest(triple))
            projection.relation(relation, triple, (entity(triple[0]), entity(triple[2])))
            projection.link("paragraph_relations", paragraph, relation)
    return projection


def _full_tables(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    tables = state.get("tables")
    if (
        state.get("schema") != "a-memorix-semantic-state"
        or state.get("schema_version") != 1
        or not isinstance(tables, dict)
    ):
        raise MemoryBundleError("不支持 state.json 的格式。")
    unsupported = sorted(
        table for table, rows in tables.items() if table not in _FULL_KEYS and rows != []
    )
    if unsupported:
        raise MemoryBundleError(f"完整包包含尚不能恢复的表：{', '.join(unsupported)}")
    result = {table: _rows(tables.get(table, []), table) for table in _FULL_KEYS}
    for table, keys in _FULL_KEYS.items():
        identities = {tuple(_key(row.get(key), key) for key in keys) for row in result[table]}
        if len(identities) != len(result[table]):
            raise MemoryBundleError(f"完整包 {table} 含重复键。")
    return result


def _key(value: object, label: str) -> str:
    return str(value) if type(value) is int else _text(value, label)


def _full_projection(bundle: MemoryBundle, state: dict[str, Any]) -> BundleProjection:
    tables = _full_tables(state)
    projection = BundleProjection(
        {}, {}, {key: tables[key] for key in PROFILE_TABLES if tables[key]}
    )
    # 保留上游 ID 和整套证据；不同版本的语义快照不覆盖彼此。
    scope = bundle.digest

    def source(table: str, key: str) -> MemorySource:
        return _source(table, key, scope=scope)

    def reference(table: str, key: str) -> MemorySource:
        target = source(table, key)
        if target not in projection.records:
            raise MemoryBundleError(f"完整包缺少关联的 {table} 原件。")
        return target

    for row in tables["paragraphs"]:
        item = source("paragraphs", row["hash"])
        status = "deleted" if row.get("is_deleted") else "active"
        projection.records[item] = _paragraph(
            item, _text(row.get("content"), "content"), row, status=status
        )
    names: dict[str, MemorySource] = {}
    for row in tables["entities"]:
        item = source("entities", row["hash"])
        name = _text(row.get("name"), "name")
        key = name.strip().lower()
        if key in names:
            raise MemoryBundleError("完整包中有无法区分的同名实体。")
        names[key] = item
        projection.entity(item, name, status="deleted" if row.get("is_deleted") else "active")
    for row in tables["relations"]:
        triple = tuple(_text(row.get(key), key) for key in ("subject", "predicate", "object"))
        subject, predicate, target = triple
        endpoints = (names.get(subject.strip().lower()), names.get(target.strip().lower()))
        if endpoints[0] is None or endpoints[1] is None:
            raise MemoryBundleError("完整包关系缺少实体端点。")
        confidence = row.get("confidence")
        if confidence is not None and type(confidence) not in (int, float):
            raise MemoryBundleError("完整包关系置信度无效。")
        projection.relation(
            source("relations", row["hash"]),
            (subject, predicate, target),
            (endpoints[0], endpoints[1]),
            status="inactive" if row.get("is_inactive") else "active",
            confidence=confidence,
        )
    for table, target_table, target_key in (
        ("paragraph_entities", "entities", "entity_hash"),
        ("paragraph_relations", "relations", "relation_hash"),
    ):
        for row in tables[table]:
            projection.link(
                table,
                reference("paragraphs", row["paragraph_hash"]),
                reference(target_table, row[target_key]),
            )

    episode_links: dict[str, list[MemoryEvidence]] = defaultdict(list)
    for row in tables["episode_paragraphs"]:
        paragraph = reference("paragraphs", row["paragraph_hash"])
        episode_links[row["episode_id"]].append(
            MemoryEvidence("paragraph", row["paragraph_hash"], paragraph)
        )
    for row in tables["episodes"]:
        item = source("episodes", row["episode_id"])
        text = _text(row.get("title"), "title") + "\n" + _text(row.get("summary"), "summary")
        record = _paragraph(item, text, row)
        projection.records[item] = replace(
            record, evidence=record.evidence + tuple(episode_links.pop(row["episode_id"], []))
        )
    if episode_links:
        raise MemoryBundleError("完整包缺少关联的 episode 原件。")
    project_facts(tables, scope, projection.records, _stamp)
    return projection


def project_bundle(bundle: MemoryBundle) -> BundleProjection:
    """验证语义闭包；拒绝无法接入真实记忆的内容。"""
    knowledge = _knowledge_docs(bundle)
    if bundle.state is None:
        projection = knowledge
    else:
        projection = _full_projection(bundle, bundle.state)
        state_paragraphs = {
            (item.key[0], record.text)
            for item, record in projection.records.items()
            if item.table == "paragraphs"
        }
        docs = {
            (doc.get("id"), doc["passage"])
            for doc in bundle.knowledge["docs"]
            if isinstance(doc.get("id"), str)
        }
        if state_paragraphs != docs or len(docs) != len(bundle.knowledge["docs"]):
            raise MemoryBundleError("完整包的知识段落和 state.json 不一致。")
    if not projection.records and not projection.profiles:
        raise MemoryBundleError("记忆包没有可安装的资料。")
    return projection
