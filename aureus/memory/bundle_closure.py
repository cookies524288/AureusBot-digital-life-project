"""从选定迁入快照重建 A_Memorix v1 的可携带语义闭包。"""

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from aureus.memory.bundle_format import paragraph_hash
from aureus.memory.bundle_projection import _FULL_KEYS
from aureus.memory.history import HistoricalMemory, MemorySource

_TARGET_TABLES = {
    "paragraph": "paragraphs",
    "entity": "entities",
    "relation": "relations",
    "episode": "episodes",
    "fact": "fact_claims",
}


class BundleProfileSource(Protocol):
    """画像子系统只提供已解析、有效且由可信来源取得的当前快照。"""

    def bundle_profile_candidates(
        self, connection: sqlite3.Connection, origins: dict[str, str], now: datetime
    ) -> list[tuple[str, dict[str, object]]]:
        """返回待核认证据闭包的画像。"""
        ...


@dataclass
class BundleClosure:
    """闭包以完整本机源键关联，生成后才换为规范的可携带键。"""

    tables: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {table: [] for table in _FULL_KEYS}
    )
    keys: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        """报告不能输出的候选数量，不暴露其内容。"""
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _timestamp(stamp: datetime | None) -> float | None:
    return stamp.timestamp() if stamp is not None else None


def _add_paragraphs(closure: BundleClosure, records: dict[str, HistoricalMemory]) -> None:
    seen: set[str] = set()
    for key, record in records.items():
        if record.source.table != "paragraphs":
            continue
        if record.status != "active" or not record.text.strip():
            closure.skip("段落没有当前正文")
            continue
        content = record.text.strip()
        identity = paragraph_hash(content)
        closure.keys[key] = identity
        if identity in seen:
            continue
        seen.add(identity)
        row = {
            "hash": identity,
            "content": content,
            "knowledge_type": "mixed",
            "event_time": _timestamp(record.recorded_at),
            "time_granularity": record.time_precision,
            "expires_at": _timestamp(record.valid_to),
        }
        closure.tables["paragraphs"].append(row)


def _graph_evidence(
    row: sqlite3.Row, records: dict[str, HistoricalMemory], closure: BundleClosure
) -> set[str]:
    paragraphs: set[str] = set()
    for link in json.loads(row["evidence"]):
        paragraph, association = link.get("source"), link.get("link_source")
        if not paragraph or not association:
            continue
        if (paragraph["instance"], paragraph["role"]) != (
            association["instance"],
            association["role"],
        ):
            continue
        paragraph_key, association_key = (
            json.dumps(
                [source[field] for field in ("instance", "role", "table", "key")],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for source in (paragraph, association)
        )
        original = records.get(association_key)
        if (
            original
            and paragraph_key in closure.keys
            and any(
                item.source and item.source.encode() == paragraph_key for item in original.evidence
            )
            and any(
                item.source and item.source.encode() == row["source_key"]
                for item in original.evidence
            )
        ):
            paragraphs.add(closure.keys[paragraph_key])
    return paragraphs


def _add_graph(
    connection: sqlite3.Connection, closure: BundleClosure, records: dict[str, HistoricalMemory]
) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'memory_graph_sources'"
        ).fetchone()
        is None
    ):
        return
    rows = connection.execute("SELECT * FROM memory_graph_sources ORDER BY kind, source_key")
    seen: set[tuple[str, str]] = set()
    links: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        key, kind = row["source_key"], row["kind"]
        if key not in records or row["status"] != "active":
            continue
        paragraphs = _graph_evidence(row, records, closure)
        if not paragraphs:
            closure.skip("图资料缺少获准的段落或关联")
            continue
        if kind == "entity":
            name = row["name"].strip()
            if not name:
                continue
            identity, table = _hash(name.lower()), "entities"
            item = {"hash": identity, "name": name}
        else:
            endpoints = (row["subject_source"], row["object_source"])
            if any(endpoint not in closure.keys for endpoint in endpoints):
                closure.skip("关系缺少获准端点")
                continue
            triple = tuple(row[field].strip() for field in ("subject", "predicate", "object"))
            if not all(triple) or any(
                closure.keys[endpoint] != _hash(name.lower())
                for endpoint, name in zip(endpoints, (triple[0], triple[2]), strict=True)
            ):
                closure.skip("关系端点与名称不一致")
                continue
            identity, table = _hash(str(triple)), "relations"
            item = dict(zip(("subject", "predicate", "object"), triple, strict=True)) | {
                "hash": identity,
                "source_paragraph": min(paragraphs),
                "retention_anchor_at": _timestamp(records[key].recorded_at) or 0,
            }
            if row["confidence"] is not None:
                item["confidence"] = row["confidence"]
        closure.keys[key] = identity
        if (table, identity) not in seen:
            closure.tables[table].append(item)
            seen.add((table, identity))
        links[kind].update((paragraph, identity) for paragraph in paragraphs)
    for kind, table in (("entity", "paragraph_entities"), ("relation", "paragraph_relations")):
        closure.tables[table] = [
            {"paragraph_hash": paragraph, f"{kind}_hash": target}
            for paragraph, target in sorted(links[kind])
        ]


def _portable_fields(record: HistoricalMemory, category: str) -> dict[str, Any]:
    for evidence in record.evidence:
        if evidence.kind == f"bundle_{category}_fields":
            value = json.loads(evidence.reference)
            if isinstance(value, dict):
                return value
    return {}


def _add_episodes(closure: BundleClosure, records: dict[str, HistoricalMemory]) -> None:
    for key, record in records.items():
        if record.source.table != "episodes" or record.status != "active":
            continue
        fields = _portable_fields(record, "episode")
        references = [link for link in record.evidence if link.kind == "paragraph"]
        if (
            not fields.get("title")
            or not fields.get("summary")
            or not references
            or any(
                not link.source
                or not _allowed_episode_link(record.source, link.source, records, closure)
                for link in references
            )
        ):
            closure.skip("情景缺少可重建正文或完整段落依据")
            continue
        identity = _hash(key)
        closure.keys[key] = identity
        paragraphs = list(
            dict.fromkeys(
                closure.keys[link.source.encode()] for link in references if link.source is not None
            )
        )
        occurred = _timestamp(record.recorded_at) or 0
        closure.tables["episodes"].append(
            {
                "episode_id": identity,
                "title": fields["title"],
                "summary": fields["summary"],
                "event_time_start": _timestamp(record.recorded_at),
                "created_at": occurred,
                "updated_at": occurred,
                "paragraph_count": len(paragraphs),
                "evidence_ids_json": json.dumps(paragraphs),
            }
        )
        closure.tables["episode_paragraphs"].extend(
            {"episode_id": identity, "paragraph_hash": paragraph, "position": position}
            for position, paragraph in enumerate(paragraphs)
        )


def _allowed_episode_link(
    episode: MemorySource,
    paragraph: MemorySource,
    records: dict[str, HistoricalMemory],
    closure: BundleClosure,
) -> bool:
    if (paragraph.instance, paragraph.role, paragraph.table) != (
        episode.instance,
        episode.role,
        "paragraphs",
    ):
        return False
    association = records.get(
        MemorySource(
            episode.instance, episode.role, "episode_paragraphs", (episode.key[0], paragraph.key[0])
        ).encode()
    )
    return bool(
        association
        and paragraph.encode() in closure.keys
        and {episode.encode(), paragraph.encode()}
        <= {item.source.encode() for item in association.evidence if item.source}
    )


def _add_facts(closure: BundleClosure, records: dict[str, HistoricalMemory]) -> None:
    discovered: dict[tuple[str, str], list[HistoricalMemory]] = defaultdict(list)
    for record in records.values():
        source = record.source
        if source.table == "fact_evidence" and len(source.key) == 4:
            discovered[source.instance, str(source.key[0])].append(record)
    for key, record in records.items():
        if record.source.table != "fact_claims":
            continue
        fields = _portable_fields(record, "fact")
        links = [link for link in record.evidence if not link.kind.startswith("bundle_")]
        if (
            record.status != "active"
            or not fields
            or not links
            or any(
                not link.source
                or link.source.encode() not in closure.keys
                or link.kind not in _TARGET_TABLES
                or link.source.table != _TARGET_TABLES[link.kind]
                for link in links
            )
        ):
            closure.skip("事实缺少可重建字段或完整证据")
            continue
        valid_links = {
            (target.kind, closure.keys[target.source.encode()], str(association.source.key[3]))
            for association in discovered[record.source.instance, str(record.source.key[0])]
            for target in association.evidence
            if target.source
            and target.kind in _TARGET_TABLES
            and target.source.table == _TARGET_TABLES[target.kind]
            and target.source.encode() in closure.keys
            and any(link.source == record.source for link in association.evidence)
        }
        expected = {
            (link.kind, closure.keys[link.source.encode()]) for link in links if link.source
        }
        if expected != {(kind, evidence) for kind, evidence, _ in valid_links}:
            closure.skip("事实证据关联已撤销或不完整")
            continue
        identity = _hash(key)
        closure.keys[key] = identity
        observed = _timestamp(record.recorded_at) or 0
        closure.tables["fact_claims"].append(
            fields
            | {
                "claim_id": identity,
                "status": "active",
                "valid_from": _timestamp(record.valid_from),
                "valid_to": _timestamp(record.valid_to),
                "first_observed_at": observed,
                "last_confirmed_at": observed,
                "created_at": observed,
                "updated_at": observed,
            }
        )
        closure.tables["fact_evidence"].extend(
            {
                "claim_id": identity,
                "evidence_type": kind,
                "evidence_id": evidence,
                "stance": stance,
                "observed_at": observed,
            }
            for kind, evidence, stance in sorted(valid_links)
        )


def add_profiles(closure: BundleClosure, candidates: list[tuple[str, dict[str, object]]]) -> None:
    """仅保留人物当前快照，所有语义引用需能唯一映射到闭包。"""
    references: dict[tuple[str, str], set[str]] = defaultdict(set)
    facts: dict[tuple[str, str], str] = {}
    for key, portable in closure.keys.items():
        source = MemorySource.decode(key)
        target = (source.instance, str(source.key[0]))
        if source.table == "fact_claims":
            facts[target] = portable
        else:
            references[target].add(portable)
    emitted: set[tuple[object, object]] = set()
    for instance, profile in candidates:
        try:
            resolved: dict[str, str] = {}
            for field in ("evidence_ids_json", "fact_claim_ids_json"):
                value = profile[field]
                identities = json.loads(value) if isinstance(value, str) else value
                if identities is None:
                    identities = []
                if not isinstance(identities, list) or not all(
                    type(key) in (str, int) for key in identities
                ):
                    raise ValueError
                linked = []
                for identity in identities:
                    key = (instance, str(identity))
                    if field == "fact_claim_ids_json":
                        linked.append(facts[key])
                    else:
                        (unique,) = references[key]
                        linked.append(unique)
                resolved[field] = json.dumps(linked)
        except (ValueError, KeyError):
            closure.skip("画像证据不在同一个获准闭包")
            continue
        if all(value == "[]" for value in resolved.values()):
            closure.skip("画像缺少可重建的证据")
            continue
        person_version = profile["person_id"], profile["profile_version"]
        if person_version in emitted:
            closure.skip("人物画像版本在所选来源重复")
            continue
        emitted.add(person_version)
        closure.tables["person_profile_snapshots"].append(
            profile | resolved | {"snapshot_id": _hash(f"{instance}\n{profile['snapshot_id']}")}
        )


def build_closure(
    connection: sqlite3.Connection, records: dict[str, HistoricalMemory], *, is_full: bool
) -> BundleClosure:
    """只用真实登记来源的当前派生对象，不从安装包正文重建。"""
    closure = BundleClosure()
    _add_paragraphs(closure, records)
    _add_graph(connection, closure, records)
    if is_full:
        _add_episodes(closure, records)
        _add_facts(closure, records)
    return closure
