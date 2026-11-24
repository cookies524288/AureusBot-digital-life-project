"""可信迁入快照的资料包范围；与查询、图展示和包安装授权独立。"""

import json
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from aureus.memory.bundle_format import MemoryBundleConflictError, bundle_digest
from aureus.memory.history import HistoricalMemory, decode_history_row

EXPORT_TABLES = frozenset(
    {
        "paragraphs",
        "entities",
        "relations",
        "paragraph_entities",
        "paragraph_relations",
        "episodes",
        "episode_paragraphs",
        "fact_claims",
        "fact_evidence",
        "image_assets",
        "image_occurrences",
        "image_observations",
        "image_memory_links",
    }
)
_SIGNATURE = """json_array(original.content, original.status, original.recorded_at,
    original.valid_from, original.valid_to, original.time_precision, original.evidence)"""


@dataclass(frozen=True)
class BundleSource:
    """可以明确选择的迁入来源标识，不公开未选择资料的名称与正文。"""

    source_id: str
    instance: str


def open_bundle_sources(connection: sqlite3.Connection) -> None:
    """建立逐项来源账，不把已有可检索资料自动登记为可选来源。"""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS memory_bundle_sources (
            instance TEXT NOT NULL, source_key TEXT NOT NULL, signature TEXT NOT NULL,
            PRIMARY KEY (instance, source_key)
        ) WITHOUT ROWID
    """)


def register_bundle_source(
    connection: sqlite3.Connection, instance: str, sources: Collection[str]
) -> None:
    """仅登记这次可信发布明确提供的记忆库来源。"""
    connection.execute("DELETE FROM memory_bundle_sources WHERE instance = ?", (instance,))
    connection.execute(
        f"""INSERT INTO memory_bundle_sources(instance, source_key, signature)
            SELECT ?, original.source_key, {_SIGNATURE} FROM memory_history original
            WHERE original.source_key IN (SELECT value FROM json_each(?))""",
        (instance, json.dumps(sorted(sources))),
    )


def list_bundle_sources(connection: sqlite3.Connection) -> list[BundleSource]:
    """列出实际登记过的来源快照，仅提供范围标识。"""
    return [
        BundleSource(bundle_digest([instance, fingerprint, keys]), instance)
        for instance, fingerprint, keys in connection.execute("""
            SELECT snapshot.instance, snapshot.fingerprint,
                   json_group_array(json_array(member.source_key, member.signature))
            FROM memory_history_snapshots snapshot
            JOIN (SELECT * FROM memory_bundle_sources ORDER BY instance, source_key) member
                USING (instance)
            GROUP BY snapshot.instance, snapshot.fingerprint ORDER BY snapshot.instance
        """)
    ]


def select_bundle_records(
    connection: sqlite3.Connection, source_ids: Sequence[str], now: datetime
) -> tuple[tuple[str, ...], dict[str, HistoricalMemory], dict[str, int]]:
    """精确选取已明确同意的来源，排除当前不可用的内容。"""
    available = {source.source_id: source.instance for source in list_bundle_sources(connection)}
    if not source_ids or len(set(source_ids)) != len(source_ids):
        raise MemoryBundleConflictError("请选择至少一个不重复的可信来源。")
    if set(source_ids) - available.keys():
        raise MemoryBundleConflictError("所选来源已变化或未登记，请重新选择。")
    instances = tuple(available[source_id] for source_id in sorted(source_ids))
    records: dict[str, HistoricalMemory] = {}
    excluded: dict[str, int] = {}
    rows = connection.execute(
        f"""
        SELECT original.*, edit.edited_content,
               member.signature = {_SIGNATURE} AS unchanged,
               (original.status IN ('active', 'missing')
                AND (original.valid_from IS NULL OR original.valid_from <= :stamp)
                AND (original.valid_to IS NULL OR original.valid_to > :stamp)
                AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)
                AND NOT coalesce(edit.is_bundle_removed, 0)) AS available
        FROM memory_bundle_sources member JOIN memory_history original USING (source_key)
        LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = original.id
        WHERE member.instance IN (SELECT value FROM json_each(:instances))
        ORDER BY original.source_key
        """,
        {"stamp": now.timestamp(), "instances": json.dumps(instances)},
    )
    for row in rows:
        if not row["unchanged"]:
            excluded["登记后原件已变化"] = excluded.get("登记后原件已变化", 0) + 1
        elif not row["available"]:
            excluded["失效、撤销或停用"] = excluded.get("失效、撤销或停用", 0) + 1
        elif row["edited_content"] is not None:
            excluded["原语义已修正，不能重建关联"] = (
                excluded.get("原语义已修正，不能重建关联", 0) + 1
            )
        else:
            records[row["source_key"]] = decode_history_row(row)
    return instances, records, excluded
