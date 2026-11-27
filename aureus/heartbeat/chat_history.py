"""从只追加的时间线和同会话原件投影聊天历史，不把草稿当作发言。"""

import json
from collections.abc import Callable

from aureus.heartbeat.events import (
    EVENT_MESSAGE,
    EVENT_REPLY_DELIVERED,
    EVENT_REPLY_DELIVERY_FAILED,
)
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.heartbeat.perception import TrustLevel
from aureus.heartbeat.reply_records import INTERNAL_REPLY_RECORD
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

CHAT_EVENTS = (EVENT_MESSAGE, EVENT_REPLY_DELIVERED, EVENT_REPLY_DELIVERY_FAILED)


def record_route(record: TimelineRecord) -> ConversationKey | None:
    """从消息元数据还原完整会话地址，无法定位时不猜。"""
    metadata = record.metadata
    if not all(
        isinstance(metadata.get(field), str) and metadata[field]
        for field in ("channel", "account_id", "message_type", "recipient_id")
    ):
        return None
    return ConversationKey(
        metadata["channel"],
        metadata["account_id"],
        metadata["message_type"],
        metadata["recipient_id"],
        metadata.get("temporary_group_id"),
    )


class ChatHistory:
    """组合时间线回执和收件箱当前可见的原件，供器官和工作记忆阅读。"""

    def __init__(
        self,
        timeline: TimelineDatabase,
        store: InboxStore | None,
        allows: Callable[[ConversationKey], bool],
    ) -> None:
        self.timeline, self.store, self.allows = timeline, store, allows

    def revision(self, scope: str) -> int | None:
        """返回会话原件的可见性版本，用于作废旧摘要。"""
        if self.store is None or not scope.startswith("["):
            return None
        address = json.loads(scope)
        if len(address) != 6 or address[1] != "qq":
            return None
        return self.store.history_revision(ConversationKey(*address[1:]))

    @staticmethod
    def _excerpt(row: InboxMessage) -> str:
        who = (
            "你"
            if row.direction == "outgoing"
            else f"{row.sender_name}（账号 {row.perception.sender_id}）"
        )
        return json.dumps({"作者": who, "原话": row.perception.content[:400]}, ensure_ascii=False)

    def reaction_target(self, record: TimelineRecord) -> InboxMessage | None:
        """反应指向同会话仍可见的己方消息；收据撤回后不再呈现。"""
        key = record_route(record)
        if key is None or self.store is None or not self.allows(key):
            return None
        target = self.store.find_platform_message(key, record.metadata.get("quoted_message_id", ""))
        if (
            target is not None
            and target.direction == "outgoing"
            and target.perception.sender_id == key.account_id
            and target.perception.visibility is record.visibility
        ):
            return target
        return None

    def references(self, record: TimelineRecord, known: set[int]) -> TimelineRecord:
        """在读取时附上仍可见的原作者、原话和不重复的邻句。"""
        if record.metadata.get("qq_social_notice") == "reaction":
            target = self.reaction_target(record)
            if target is not None:
                reference = "表情回应的原件：" + self._excerpt(target)
                return record.model_copy(
                    update={
                        "metadata": {
                            **record.metadata,
                            "chat_references": [reference],
                        }
                    }
                )
            return record
        key = record_route(record)
        store = self.store
        if (
            key is None
            or store is None
            or not self.allows(key)
            or not record.metadata.get("inbox_message_id")
        ):
            return record
        source = store.find_platform_message(key, record.metadata.get("message_id", ""))
        if (
            source is None
            or source.id != record.metadata["inbox_message_id"]
            or source.perception.visibility is not record.visibility
        ):
            return record
        quote = source.perception.quoted_message_id
        references = []
        original = store.find_platform_message(key, quote) if quote else None
        if (
            original is not None
            and original.perception.visibility is record.visibility
            and original.perception.trust is not TrustLevel.UNVERIFIED
        ):
            references.append("引用原件（并非本条发言者）：" + self._excerpt(original))
        if source.perception.mentioned or (
            original is not None
            and original.direction == "outgoing"
            and original.perception.sender_id == key.account_id
        ):
            references.extend(
                "此前邻句（仅作背景，不是回复候选）：" + self._excerpt(row)
                for row in store.nearby_before(source, 3)
                if row.id not in known and row != original
            )
        if not references:
            return record
        return record.model_copy(
            update={"metadata": {**record.metadata, "chat_references": references}}
        )

    async def project(self, record: TimelineRecord) -> TimelineRecord | None:
        """把成功段投影为发言，未确认段仅提示状态，撤回的原件不可见。"""
        if record.event_type not in CHAT_EVENTS:
            return record
        meta = record.metadata
        key = record_route(record)
        if key is not None and key.platform == "qq" and not self.allows(key):
            return None
        if meta.get("qq_social_notice") == "reaction" and self.reaction_target(record) is None:
            return None
        if meta.get(INTERNAL_REPLY_RECORD):
            total = len(meta.get("reply_segments") or [None])
            settled = await self.timeline.async_query(
                TimelineQuery(
                    event_types=CHAT_EVENTS[1:], metadata={"reply_id": record.id}, limit=-1
                )
            )
            pending = total - len(
                {
                    item.metadata["segment"]
                    for item in settled
                    if type(item.metadata.get("segment")) is int
                }
            )
            if pending <= 0 or meta.get("nontext_reply"):
                return None
            return self._status(
                record, f"另有 {pending} 段回复待确认，不能当作已发，也不必重复补发。"
            )
        if record.event_type == EVENT_REPLY_DELIVERY_FAILED:
            return self._status(record, "一段回复没有获确认，不能当作已发。")
        outgoing = record.event_type == EVENT_REPLY_DELIVERED
        if (
            self.store is not None
            and key is not None
            and key.platform == "qq"
            and (outgoing or "inbox_message_id" in meta)
        ):
            original = self.store.find_platform_message(key, meta.get("message_id", ""))
            if not self.allows(key):
                return None
            if original is None or original.perception.visibility is not record.visibility:
                return None
            if outgoing and (
                original.direction != "outgoing" or original.perception.sender_id != key.account_id
            ):
                return None
            if not outgoing and (
                original.direction != "incoming" or original.id != meta.get("inbox_message_id")
            ):
                return None
        return record.model_copy(update={"event_type": EVENT_MESSAGE}) if outgoing else record

    async def pending(
        self, key: ConversationKey, visibility: VisibilityScope, limit: int
    ) -> list[TimelineRecord]:
        """未结清草稿只投影状态，不挤占工作记忆压缩和学习的经历数。"""
        drafts = await self.timeline.async_query(
            TimelineQuery(
                event_type=EVENT_MESSAGE,
                visibility=visibility,
                latest=True,
                limit=limit,
                metadata={
                    INTERNAL_REPLY_RECORD: True,
                    "channel": key.platform,
                    "account_id": key.account_id,
                    "message_type": key.kind,
                    "recipient_id": key.recipient_id,
                    "temporary_group_id": key.temporary_group_id,
                },
            )
        )
        return [status for draft in drafts if (status := await self.project(draft)) is not None]

    @staticmethod
    def _status(record: TimelineRecord, text: str) -> TimelineRecord:
        route = {
            key: record.metadata[key]
            for key in (
                "channel",
                "account_id",
                "recipient_id",
                "message_type",
                "temporary_group_id",
            )
            if key in record.metadata
        }
        return record.model_copy(
            update={
                "event_type": EVENT_MESSAGE,
                "actor_type": ActorType.SYSTEM,
                "content": text,
                "metadata": {**route, "delivery_status": True},
            }
        )
