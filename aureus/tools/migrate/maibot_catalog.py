"""MaiBot 迁移包的业务表分类、来源键和原值编码。"""

import base64
import json
import math
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from aureus.tools.migrate.maibot import quote_identifier, read_schema_objects

# 主库和记忆库名称来自 MaiBot v1.3.15 的 database_model、metadata_schema 与 metadata_fact。
BUSINESS_TABLES = {
    "main": frozenset(
        {
            "chat_sessions",
            "person_info",
            "mai_messages",
            "expressions",
            "jargons",
            "high_frequency_terms",
            "bot_platform_accounts",
            "images",
            "binary_data",
        }
    ),
    "memory": frozenset(
        {
            "paragraphs",
            "fact_claims",
            "fact_evidence",
            "fact_transitions",
            "entities",
            "relations",
            "deleted_relations",
            "relation_hash_aliases",
            "paragraph_relations",
            "paragraph_entities",
            "episodes",
            "episode_paragraphs",
            "episode_rebuild_sources",
            "person_profile_snapshots",
            "person_profile_active_persons",
            "image_assets",
            "image_occurrences",
            "image_observations",
            "image_memory_links",
            "external_memory_refs",
            "delete_operations",
            "delete_operation_items",
            "knowledge_packages",
            "knowledge_package_paragraphs",
            "knowledge_package_resources",
        }
    ),
    "people": frozenset(
        {"person_state", "relation_edge", "thread_item", "events", "daily_schedule"}
    ),
}

_OTHER_TABLES = {
    "main": {
        "statistics": frozenset(
            {
                "model_task_usage",
                "llm_usage",
                "tool_records",
                "maisaka_monitor_events",
                "online_time",
            }
        ),
        "rebuildable": frozenset(
            {
                "statistics_aggregation_cursors",
                "statistics_message_hourly",
                "statistics_tool_hourly",
                "statistics_model_hourly",
            }
        ),
        "runtime": frozenset({"one_time_maintenance_tasks"}),
    },
    "memory": {
        "rebuildable": frozenset({"image_embeddings", "paragraph_vector_backfill"}),
        "configuration": frozenset(
            {
                "person_profile_switches",
                "person_profile_overrides",
                "person_profile_alias_overrides",
            }
        ),
        "runtime": frozenset(
            {
                "schema_migrations",
                "chat_summary_checkpoints",
                "episode_empty_groups",
                "image_description_compensations",
                "image_pending_descriptions",
                "image_embedding_jobs",
                "image_runtime_state",
                "memory_feedback_tasks",
                "memory_feedback_action_logs",
                "paragraph_stale_relation_marks",
                "person_profile_refresh_queue",
                "relation_graph_projection_jobs",
                "storage_cleanup_jobs",
                "memory_fuzzy_modify_plans",
                "memory_v5_operations",
            }
        ),
    },
    "people": {},
}

_EXTERNAL_COLUMNS = frozenset(
    {
        "full_path",
        "file_path",
        "image_path",
        "media_path",
        "local_path",
        "relative_path",
        "source_path",
        "path",
        "file_url",
        "image_url",
        "media_url",
        "url",
        "uri",
        "attachments",
        "external_ref",
        "external_refs",
    }
)
_MEDIA_COLUMNS = {
    "mai_messages": frozenset({"content", "message_segments", "message_components"}),
    "paragraphs": frozenset({"source", "source_ref", "metadata"}),
    "image_assets": frozenset({"file_hash", "payload", "storage_key"}),
    "external_memory_refs": frozenset({"external_id", "metadata_json"}),
    "knowledge_package_resources": frozenset({"resource_key"}),
}

type SqlValue = int | float | str | bytes | None

STAGING_APPLICATION_ID = 0x41554D42  # AUMB，供显式识别迁移暂存库。


@dataclass(frozen=True)
class Table:
    """源库普通表及其可重复使用的来源键定义。"""

    number: int
    name: str
    category: str
    reason: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]


class MigrationError(Exception):
    """可公开的错误代码与包内位置，不包含原始数据。"""

    def __init__(self, code: str, *, table: int | None = None, row: int | None = None) -> None:
        """记录固定错误代码及可选的表、行编号。"""
        super().__init__(code)
        self.code = code
        self.table = table
        self.row = row


def encode_json(value: object) -> bytes:
    """用固定字节表示编码清单、来源键或记录。"""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def _encode_value(value: SqlValue) -> object:
    if isinstance(value, bytes):
        return {"blob_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"real": str(value)}
    return value


def _classify_table(role: str, name: str, has_key: bool) -> tuple[str, str]:
    if name in BUSINESS_TABLES[role]:
        return ("export", "recognized") if has_key else ("unmapped", "no_primary_key")
    for category, names in _OTHER_TABLES[role].items():
        if name in names:
            return category, category
    return "unmapped", "unknown_table"


def read_tables(database: sqlite3.Connection, role: str) -> Iterator[Table]:
    """读取全部普通表，并按角色识别可导出的业务表。"""
    names = sorted(name for name, kind in read_schema_objects(database).items() if kind == "table")
    for number, name in enumerate(names, start=1):
        columns = [
            (column, rank)
            for _, column, _, _, _, rank, hidden in database.execute(
                "SELECT * FROM pragma_table_xinfo(?, 'main')", (name,)
            )
            if hidden in (0, 3)
        ]
        key = tuple(column for column, rank in sorted(columns, key=lambda item: item[1]) if rank)
        category, reason = _classify_table(role, name.casefold(), bool(key))
        yield Table(number, name, category, reason, tuple(column for column, _ in columns), key)


def count_rows(database: sqlite3.Connection, table: Table) -> int:
    """统计仅保留在快照中的普通表。"""
    return database.execute(f"SELECT COUNT(*) FROM main.{quote_identifier(table.name)}").fetchone()[
        0
    ]


def read_rows(
    database: sqlite3.Connection, table: Table
) -> Iterator[tuple[tuple[SqlValue, ...], dict[str, SqlValue]]]:
    """按声明主键低内存读取业务行，保留 SQLite 原值和键顺序。"""
    columns = ", ".join(map(quote_identifier, table.columns))
    order = ", ".join(map(quote_identifier, table.primary_key))
    # 两个 NULL 主键行没有可用来源键。其余行有唯一顺序，便于流式核验包。
    for values in database.execute(
        f"SELECT {columns} FROM main.{quote_identifier(table.name)} ORDER BY {order}"
    ):
        row = dict(zip(table.columns, values, strict=True))
        yield tuple(row[column] for column in table.primary_key), row


def encode_row(
    table: Table, key: tuple[SqlValue, ...], row: dict[str, SqlValue]
) -> tuple[bytes, bytes]:
    """编码主键及包含原值和待处理引用的 JSON Lines 记录。"""
    values = {name: _encode_value(value) for name, value in row.items()}
    reference_columns = _EXTERNAL_COLUMNS | _MEDIA_COLUMNS.get(table.name.casefold(), frozenset())
    references = {
        name: values[name]
        for name, value in row.items()
        if name.casefold() in reference_columns and value is not None and value != ""
    }
    encoded_key = [_encode_value(value) for value in key]
    line = {
        "table": table.number,
        "key": encoded_key,
        "values": values,
        "external_refs": references,
    }
    # MaiBot v1.3.15 的 raw_content 是 MsgPack。保留原值并单列待解析字段。
    if table.name.casefold() == "mai_messages" and row.get("raw_content"):
        line["deferred_media_fields"] = ["raw_content"]
    return encode_json(encoded_key), encode_json(line) + b"\n"
