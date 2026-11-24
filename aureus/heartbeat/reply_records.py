"""将通道逐段投递的事实追加到时间线（3.15），不改写已存在的回复记录。"""

from collections.abc import Callable

from aureus.heartbeat.events import (
    EVENT_RECALL_REPLY_DELIVERED,
    EVENT_REPLY_DELIVERED,
    EVENT_REPLY_DELIVERY_FAILED,
)
from aureus.timeline import ActorType, TimelineDatabase, TimelineRecord

INTERNAL_REPLY_RECORD = "outbox_internal"


def delivery_callbacks(
    timeline: TimelineDatabase, reply: TimelineRecord, content: str, segment: int | None,
) -> tuple[Callable[[], None], Callable[[str], None]]:
    """最多保留一个投递结果；网页同一条发给多个连接时不重复记录。"""
    fields = {"channel": reply.metadata.get("channel", ""), "reply_id": reply.id}
    if "round_id" in reply.metadata:
        fields["round_id"] = reply.metadata["round_id"]
    if segment is not None:
        fields["segment"] = segment
    settled = False

    def confirm() -> None:
        nonlocal settled
        if settled:
            return
        timeline.append(
            EVENT_REPLY_DELIVERED, content, ActorType.ME,
            visibility=reply.visibility, metadata=fields,
        )
        settled = True
        if segment is not None and reply.metadata.get("recall_feedback_refs"):
            timeline.append(
                EVENT_RECALL_REPLY_DELIVERED, "一段带召回参考的回复已投递。", ActorType.SYSTEM,
                visibility=reply.visibility,
                metadata={"reply_id": reply.id, "segment": segment},
            )

    def fail(reason: str) -> None:
        nonlocal settled
        if settled:
            return
        timeline.append(
            EVENT_REPLY_DELIVERY_FAILED, reason, ActorType.SYSTEM,
            visibility=reply.visibility, metadata=fields,
        )
        settled = True

    return confirm, fail
