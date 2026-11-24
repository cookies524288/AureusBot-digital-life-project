"""从当前可参与召回的段落及关联生成固定版知识包。"""

import json
import sqlite3
from datetime import datetime
from typing import Any, BinaryIO

from aureus.memory.bundle_format import (
    BUNDLE_FORMAT,
    MemoryBundle,
    MemoryBundleConflictError,
    bundle_digest,
    paragraph_hash,
    write_bundle,
)
from aureus.memory.history import decode_history_row


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
        """
        SELECT graph.* FROM memory_graph_sources graph
        JOIN memory_history original USING (source_key)
        LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = original.id
        WHERE graph.status = 'active'
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
