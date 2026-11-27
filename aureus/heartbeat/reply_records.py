"""将通道逐段投递的事实追加到时间线（3.15），不改写已存在的回复记录。"""

from collections.abc import Callable
from typing import Any, Protocol

from aureus.heartbeat.events import (
    EVENT_RECALL_REPLY_DELIVERED,
    EVENT_REPLY_DELIVERED,
    EVENT_REPLY_DELIVERY_FAILED,
)
from aureus.heartbeat.round_timing import RoundTiming
from aureus.timeline import ActorType, TimelineDatabase, TimelineRecord

INTERNAL_REPLY_RECORD = "outbox_internal"


class DeliveryConfirm(Protocol):
    """平台确认某段送达；能返回原件号的通道一并传入。"""

    def __call__(self, platform_message_id: str = "") -> None:
        """仅在收到肯定回执后调用。"""
        ...


def delivery_callbacks(
    timeline: TimelineDatabase,
    reply: TimelineRecord,
    content: str,
    segment: int | None,
    *,
    on_confirm: Callable[[], None] | None = None,
    on_failure: Callable[[], None] | None = None,
    timing: RoundTiming | None = None,
) -> tuple[DeliveryConfirm, Callable[[str], None]]:
    """最多保留一个投递结果；网页同一条发给多个连接时不重复记录。"""
    fields: dict[str, Any] = {"channel": reply.metadata.get("channel", ""), "reply_id": reply.id}
    for key in (
        "round_id",
        "account_id",
        "recipient_id",
        "message_type",
        "temporary_group_id",
        "to_user",
        "chat_play",
    ):
        if key in reply.metadata:
            fields[key] = reply.metadata[key]
    if segment is not None:
        fields["segment"] = segment
        fields["segment_total"] = len(reply.metadata.get("reply_segments") or [content])
    settled = False

    def confirm(platform_message_id: str = "") -> None:
        nonlocal settled
        if settled:
            return
        details = fields.copy()
        if platform_message_id:
            details["message_id"] = platform_message_id
        if timing is not None:
            total = 0 if reply.metadata.get("nontext_reply") else len(
                reply.metadata.get("reply_segments") or [reply.content]
            )
            details["stage_timings"] = timing.observe_delivery(segment, total)
        timeline.append(
            EVENT_REPLY_DELIVERED,
            content,
            ActorType.ME,
            visibility=reply.visibility,
            metadata=details,
        )
        settled = True
        if on_confirm is not None:
            on_confirm()
        if segment is not None and reply.metadata.get("recall_feedback_refs"):
            timeline.append(
                EVENT_RECALL_REPLY_DELIVERED,
                "一段带召回参考的回复已投递。",
                ActorType.SYSTEM,
                visibility=reply.visibility,
                metadata={"reply_id": reply.id, "segment": segment},
            )

    def fail(reason: str) -> None:
        nonlocal settled
        if settled:
            return
        timeline.append(
            EVENT_REPLY_DELIVERY_FAILED,
            reason,
            ActorType.SYSTEM,
            visibility=reply.visibility,
            metadata=fields,
        )
        settled = True
        if on_failure is not None:
            on_failure()

    return confirm, fail
