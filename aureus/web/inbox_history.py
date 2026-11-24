"""组合平台原件和兼容的历史来源，不从记忆索引取正文。"""

from dataclasses import asdict
from typing import Any

from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.memory import HistoricalMemory
from aureus.timeline import TimelineRecord
from aureus.timeline.platform_history import LegacyPlatformHistory

LEGACY_SCAN_SIZE = 100


def inbox_message(message: InboxMessage, *, include_attachment_ids: bool = True) -> dict[str, Any]:
    """返回有规范回址的收发原件。"""
    perception = message.perception
    return {
        "id": message.id,
        "source": "inbox",
        "platform": message.conversation.platform,
        "conversation": asdict(message.conversation),
        "sender_id": perception.sender_id,
        "sender_name": message.sender_name,
        "direction": message.direction,
        "platform_message_id": perception.route.message_id if perception.route else None,
        "visibility": perception.visibility,
        "quoted_message_id": perception.quoted_message_id,
        "attachments": [
            asdict(item) if include_attachment_ids else {"kind": item.kind, "is_emoji": item.is_emoji}
            for item in perception.attachments
        ],
        "timestamp": message.timestamp,
        "content": perception.content,
    }


def timeline_message(record: TimelineRecord) -> dict[str, Any]:
    """保留旧入站记录中的事实，不恢复未保存的附件。"""
    metadata = record.metadata
    return {
        "id": record.id,
        "source": "timeline",
        "platform": metadata.get("channel"),
        "sender_id": record.actor_id,
        "sender_name": metadata.get("sender_name"),
        "direction": "incoming",
        "platform_message_id": metadata.get("message_id"),
        "visibility": record.visibility,
        "quoted_message_id": metadata.get("quoted_message_id"),
        "attachments": [],
        "timestamp": record.timestamp,
        "content": record.content,
    }


def imported_message(record: HistoricalMemory) -> dict[str, Any]:
    """保留导入原始定位，不把原表主键等同平台消息号。"""
    return {
        "id": record.entry_id,
        "source": "imported",
        "platform": None,
        "source_reference": record.source.encode(),
        "sender_name": record.actor_label,
        "sender_id": None,
        "platform_message_id": None,
        "visibility": None,
        "attachments": [],
        "timestamp": record.recorded_at,
        "received_at": record.received_at,
        "content": record.text,
    }


def timeline_page(
    store: InboxStore,
    history: LegacyPlatformHistory,
    key: ConversationKey,
    *,
    before_id: int | None,
    limit: int,
    skip: int = 0,
) -> list[TimelineRecord]:
    """跨过已有规范原件的旧记录，再对剩余记录翻页。"""
    rows: list[TimelineRecord] = []
    scope = (key.platform, key.account_id, key.kind, key.recipient_id, key.temporary_group_id)
    while len(rows) < limit:
        remaining = limit - len(rows)
        batch_size = max(remaining, min(skip + remaining, LEGACY_SCAN_SIZE))
        page = history.page(scope, before_id=before_id, limit=batch_size)
        identifiers = [row.metadata.get("message_id") for row in page]
        canonical = store.existing_message_ids(
            key, (value for value in identifiers if isinstance(value, str) and value)
        )
        unique = [
            row for row, value in zip(page, identifiers, strict=True)
            if not isinstance(value, str) or value not in canonical
        ]
        rows.extend(unique[skip : skip + remaining])
        skip = max(0, skip - len(unique))
        if len(page) < batch_size:
            break
        before_id = page[-1].id
    return rows
