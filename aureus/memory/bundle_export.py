"""从当前可参与召回的段落及关联生成固定版知识包。"""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from aureus.memory.bundle_closure import BundleProfileSource, add_profiles, build_closure
from aureus.memory.bundle_format import (
    BUNDLE_FORMAT,
    ContentLevel,
    MemoryBundle,
    MemoryBundleConflictError,
    bundle_digest,
    paragraph_hash,
    write_bundle,
)
from aureus.memory.bundle_image_export import export_images
from aureus.memory.bundle_images import canonical_image_semantics
from aureus.memory.bundle_sources import select_bundle_records
from aureus.memory.history import decode_history_row
from aureus.memory.message_boundary import is_message_source


def _paragraph_docs(
    connection: sqlite3.Connection, now: float
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    docs: dict[str, dict[str, Any]] = {}
    by_source: dict[str, list[dict[str, Any]]] = {}
    rows = connection.execute(
        """
        SELECT original.*, edit.edited_content FROM memory_history original
        LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = original.id
        WHERE json_extract(original.source_key, '$[2]') = 'paragraphs'
            AND original.status = 'active'
            AND (original.valid_from IS NULL OR original.valid_from <= ?)
            AND (original.valid_to IS NULL OR original.valid_to > ?)
            AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)
            AND NOT coalesce(edit.is_bundle_removed, 0)
        ORDER BY original.id
    """,
        (now, now),
    )
    for row in rows:
        record = decode_history_row(row)
        text = (row["edited_content"] if row["edited_content"] is not None else record.text).strip()
        if not text:
            continue
        identity = paragraph_hash(text)
        source = next(
            (item.reference for item in record.evidence if item.kind == "maibot_source"), ""
        )
        conversation = next(
            (item.reference for item in record.evidence if item.kind == "maibot_conversation"), ""
        )
        time_meta: dict[str, Any] = {}
        if record.recorded_at is not None:
            time_meta["event_time"] = record.recorded_at.timestamp()
        if record.time_precision:
            time_meta["time_granularity"] = record.time_precision
        doc = docs.setdefault(
            identity,
            {
                "id": identity,
                "passage": text,
                "extracted_entities": [],
                "extracted_triples": [],
                "a_memorix": {
                    "knowledge_type": "mixed",
                    "source": source,
                    "time_meta": time_meta,
                    "metadata": {"chat_id": conversation} if conversation else {},
                },
            },
        )
        if row["edited_content"] is None:
            by_source.setdefault(row["source_key"], []).append(doc)
    return list(docs.values()), by_source


def _encoded(location: dict[str, Any]) -> str:
    return json.dumps(
        [location[field] for field in ("instance", "role", "table", "key")],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _attach_graph(
    connection: sqlite3.Connection, by_source: dict[str, list[dict[str, Any]]], now: float
) -> None:
    if (
        connection.execute("""
        SELECT 1 FROM sqlite_schema WHERE name = 'memory_graph_sources'
    """).fetchone()
        is None
    ):
        return
    rows = connection.execute(
        f"""
        SELECT graph.* FROM memory_graph_sources graph
        JOIN memory_history original USING (source_key)
        LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = original.id
        WHERE graph.status = 'active'
            AND NOT ({is_message_source("original.source_key")})
            AND original.status IN ('active', 'missing')
            AND (original.valid_from IS NULL OR original.valid_from <= ?)
            AND (original.valid_to IS NULL OR original.valid_to > ?)
            AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)
            AND NOT coalesce(edit.is_bundle_removed, 0) AND edit.edited_content IS NULL
        ORDER BY graph.source_key
    """,
        (now, now),
    ).fetchall()
    entities = {row["source_key"] for row in rows if row["kind"] == "entity"}
    for row in rows:
        if row["kind"] == "entity":
            column, item = "extracted_entities", row["name"]
        elif row["subject_source"] in entities and row["object_source"] in entities:
            column, item = (
                "extracted_triples",
                [row[name] for name in ("subject", "predicate", "object")],
            )
        else:
            continue
        for link in json.loads(row["evidence"]):
            if link["source"] is None:
                continue
            for doc in by_source.get(_encoded(link["source"]), ()):
                if item not in doc[column]:
                    doc[column].append(item)


def export_knowledge(connection: sqlite3.Connection, output: BinaryIO, now: datetime) -> None:
    """按 A_Memorix v1 LPMM 格式导出当前段落及有证据的图关系。"""
    with connection:
        connection.execute("BEGIN")
        docs, by_source = _paragraph_docs(connection, now.timestamp())
        if not docs:
            raise MemoryBundleConflictError("没有可导出的有效知识段落。")
        _attach_graph(connection, by_source, now.timestamp())
    knowledge = {"format": "lpmm_openie", "format_version": 1, "docs": docs}
    digest = bundle_digest({"knowledge": knowledge, "state": None})
    package = {"id": f"amx.{digest[:16]}", "version": "1.0.0", "name": "Aureus 知识包"}
    entities = {entity for doc in docs for entity in doc["extracted_entities"]}
    triples = {tuple(triple) for doc in docs for triple in doc["extracted_triples"]}
    manifest = {
        "format": BUNDLE_FORMAT,
        "format_version": 1,
        "content_level": "knowledge",
        "created_at": now.isoformat(),
        "content_digest": digest,
        "package": package,
        "selector": {"type": "all"},
        "components": ["knowledge.json"],
        "counts": {"paragraphs": len(docs), "entities": len(entities), "relations": len(triples)},
        "embedding_fingerprint": None,
        "compatibility": {
            "knowledge_semantics": "lpmm_openie",
            "minimum_a_memorix_bundle_version": 1,
        },
    }
    write_bundle(
        MemoryBundle(
            package["id"],
            package["version"],
            package["name"],
            "knowledge",
            digest,
            manifest,
            knowledge,
            None,
        ),
        output,
    )


_EXCLUDED = (
    "不是全库备份。不包含消息正文、已安装包原件、账号绑定、后台任务、审计和实例配置。",
    "不包含不能确认来源或完整重建的资料、手工画像覆盖和本地别名。",
    "在线总结和未登记的旧迁入快照没有此入口的导出范围。",
    "只有明确选中且可核验的归档图片能进入图片扩展。不导出在线对话图片和向量。",
)


def _build_docs(tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    names = {row["hash"]: row["name"] for row in tables["entities"]}
    triples = {
        row["hash"]: [row[key] for key in ("subject", "predicate", "object")]
        for row in tables["relations"]
    }
    entities: dict[str, list[str]] = defaultdict(list)
    relations: dict[str, list[list[str]]] = defaultdict(list)
    for row in tables["paragraph_entities"]:
        entities[row["paragraph_hash"]].append(names[row["entity_hash"]])
    for row in tables["paragraph_relations"]:
        relations[row["paragraph_hash"]].append(triples[row["relation_hash"]])
    return [
        {
            "id": row["hash"],
            "passage": row["content"],
            "extracted_entities": entities[row["hash"]],
            "extracted_triples": relations[row["hash"]],
            "a_memorix": {
                "knowledge_type": row["knowledge_type"],
                "source": row.get("source", ""),
                "time_meta": {
                    name: row[name]
                    for name in ("event_time", "time_granularity")
                    if row.get(name) is not None
                },
                "metadata": {},
            },
        }
        for row in sorted(tables["paragraphs"], key=lambda row: row["hash"])
    ]


def generate_bundle(
    connection: sqlite3.Connection,
    source_ids: Sequence[str],
    level: ContentLevel,
    now: datetime,
    people: BundleProfileSource | None,
    *,
    output: BinaryIO | None = None,
    expected_digest: str | None = None,
    include_images: bool = False,
    attachment_root: Path | None = None,
) -> dict[str, Any]:
    """预检或生成选定闭包；下载时需要与同范围的最新预检匹配。"""
    with connection:
        connection.execute("BEGIN")
        instances, records, excluded = select_bundle_records(connection, source_ids, now)
        closure = build_closure(connection, records, is_full=level == "full")
        if level == "full" and people is not None:
            add_profiles(closure, people.bundle_profile_candidates(connection, instances, now))
    knowledge = {"format": "lpmm_openie", "format_version": 1, "docs": _build_docs(closure.tables)}
    state = (
        {
            "schema": "a-memorix-semantic-state",
            "schema_version": 1,
            "tables": closure.tables,
            "excluded": list(_EXCLUDED),
        }
        if level == "full"
        else None
    )
    images, binary_members = (
        export_images(records, closure, attachment_root, is_full=level == "full")
        if include_images
        else (None, {})
    )
    semantic_payload = {"knowledge": knowledge, "state": state}
    if images is not None:
        semantic_payload["images"] = canonical_image_semantics(images)
    version_number = 2 if images is not None else 1
    digest = bundle_digest(semantic_payload)
    permit = bundle_digest([sorted(source_ids), level, include_images, digest])
    counts = {
        table: len(rows)
        for table, rows in closure.tables.items()
        if state is not None or table in ("paragraphs", "entities", "relations")
    }
    package = {
        "id": f"amx.{digest[:16]}",
        "version": "1.0.0",
        "name": "Aureus 完整语义包" if state is not None else "Aureus 知识包",
    }
    components = sorted(
        [
            "knowledge.json",
            *(["state.json"] if state is not None else []),
            *(["images.json", *binary_members] if images is not None else []),
        ]
    )
    if images is not None:
        counts.update(
            {
                f"image_{key}": len(images[key])
                for key in ("assets", "occurrences", "observations", "links")
            }
        )
    manifest = {
        "format": BUNDLE_FORMAT,
        "format_version": version_number,
        "content_level": level,
        "created_at": now.isoformat(),
        "content_digest": digest,
        "package": package,
        "selector": {"type": "all"},
        "components": components,
        "counts": counts,
        "embedding_fingerprint": None,
        "compatibility": {
            "knowledge_semantics": "lpmm_openie",
            "minimum_a_memorix_bundle_version": 1,
        },
    }
    bundle = MemoryBundle(
        package["id"],
        package["version"],
        package["name"],
        level,
        digest,
        manifest,
        knowledge,
        state,
        images,
        binary_members,
    )
    can_export = bool(knowledge["docs"]) or images is not None
    if output is not None:
        if not can_export or expected_digest != permit:
            raise MemoryBundleConflictError("没有可导出的资料，或闭包已变化；请重新预检。")
        write_bundle(bundle, output)
    return {
        "content_level": level,
        "format_version": version_number,
        "counts": counts,
        "can_export": can_export,
        "excluded": excluded | closure.skipped,
        "limitations": list(_EXCLUDED),
        "content_digest": digest,
        "preview_digest": permit,
        "components": components,
        "installation_id": bundle.installation_id,
    }
