"""从已暂存的 MaiBot 原件补录记忆流的时间及会话依据。"""

import json
from collections import defaultdict
from collections.abc import Iterator
from contextlib import closing
from datetime import tzinfo
from pathlib import Path

from aureus.memory import MemoryEvent, MemoryEventType, MemoryIndex, MemorySnapshotConflictError
from aureus.tools.migrate.maibot import open_readonly_database
from aureus.tools.migrate.maibot_catalog import MigrationError
from aureus.tools.migrate.maibot_memory import _text, _time
from aureus.tools.migrate.maibot_memory_rows import MemoryRows, StagedValue

_CHAT_SOURCES = {"chat_summary", "memory", "chat_stream", "chat_history", "maibot.chat_history"}


def _decode(value: StagedValue) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _chat_sources(value: object, references: dict[str, set[str]]) -> set[str]:
    if isinstance(value, dict):
        chats = {
            str(value[key]).strip()
            for key in ("chat_id", "session_id", "chat_stream_id")
            if type(value.get(key)) in (str, int) and str(value[key]).strip()
        }
        for item in value.values():
            chats.update(_chat_sources(item, references))
        return chats
    if isinstance(value, list):
        return {chat for item in value for chat in _chat_sources(item, references)}
    if isinstance(value, str):
        source, separator, chat = value.strip().partition(":")
        if separator and source in _CHAT_SOURCES and chat:
            return {chat}
        return references.get(value, set())
    return set()


def _attributed_events(rows: MemoryRows, zone: tzinfo) -> Iterator[MemoryEvent]:
    references: dict[str, set[str]] = {}
    for row in rows.rows("memory", "paragraphs"):
        references[str(row.source.key[0])] = _chat_sources(
            [_decode(row.values.get("metadata")), row.values.get("source")], {}
        )
    for row in rows.rows("memory", "relations"):
        values = row.values
        chats = _chat_sources(values.get("source_paragraph"), references)
        references[str(row.source.key[0])] = chats
        stamp, _ = _time(values.get("inactive_since"), zone)
        if stamp is not None or _text(values, "inactive_reason") == "decay":
            summary = " ".join(_text(values, part) for part in ("subject", "predicate", "object"))
            yield MemoryEvent(
                "relation_frozen",
                stamp,
                summary.strip() or "关系已冻结",
                tuple(sorted(chats)),
                row.source,
            )

    for row in rows.rows("memory", "person_profile_snapshots"):
        values = row.values
        chats = _chat_sources(_decode(values.get("evidence_ids_json")), references)
        version = values.get("profile_version")
        event_type: MemoryEventType = "profile_snapshot"
        if version == 1:
            event_type = "profile_created"
        elif isinstance(version, int) and version > 1:
            event_type = "profile_updated"
        stamp, _ = _time(values.get("updated_at"), zone)
        subject = _text(values, "person_id") or "人物"
        summary = f"{subject} · 画像版本 {version if isinstance(version, int) else '未记录'}"
        yield MemoryEvent(event_type, stamp, summary, tuple(sorted(chats)), row.source)

    items: defaultdict[str, set[str]] = defaultdict(set)
    for row in rows.rows("memory", "delete_operation_items"):
        values = row.values
        items[_text(values, "operation_id")].update(
            _chat_sources([_decode(value) for value in values.values()], references)
        )
    for row in rows.rows("memory", "delete_operations"):
        values = row.values
        summary = _text(values, "reason") or f"删除模式：{_text(values, 'mode') or '未知'}"
        chats = items[str(row.source.key[0])] | _chat_sources(
            [_decode(values.get("selector")), _decode(values.get("summary_json"))], references
        )
        created, _ = _time(values.get("created_at"), zone)
        restored, _ = _time(values.get("restored_at"), zone)
        yield MemoryEvent("delete_executed", created, summary, tuple(sorted(chats)), row.source)
        if restored is not None or _text(values, "status") == "restored":
            yield MemoryEvent(
                "delete_restored", restored, "已恢复这次删除操作", tuple(sorted(chats)), row.source
            )


def publish_maibot_memory_events(
    staging: Path, instance: str, zone: tzinfo, index: MemoryIndex
) -> dict[str, object]:
    """由已发布的同一暂存快照补齐事件，不更改既存记忆。"""
    with closing(open_readonly_database(staging)) as connection:
        connection.execute("BEGIN")
        rows = MemoryRows(connection, instance)
        try:
            index.open()
            added = index.publish_events(
                _attributed_events(rows, zone), (instance, rows.fingerprint(str(zone)))
            )
        except MemorySnapshotConflictError as error:
            raise MigrationError("memory_snapshot_conflict") from error
        finally:
            index.close()
    return {"operation": "publish-memory-events", "added": added}
