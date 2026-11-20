"""从只追加的送达收据核实消息，不把发送意图当成已说出口（D76）。"""

from aureus.heartbeat.events import EVENT_MESSAGE_DELIVERY
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, TimelineRecord


def read_delivery_receipt(
    timeline: TimelineDatabase, message: TimelineRecord
) -> TimelineRecord | None:
    """按来源读实际送达收据，时间戳是通道返回后注入时钟的时刻。"""
    receipts = timeline.query(
        TimelineQuery(
            event_type=EVENT_MESSAGE_DELIVERY,
            actor_type=ActorType.ME,
            metadata={"message_id": message.id},
            limit=1,
        )
    )
    return receipts[0] if receipts else None


def read_message_delivery(timeline: TimelineDatabase, message: TimelineRecord) -> bool | None:
    """按来源读真实通道结果；没有收据就是未知，不能假定成功。"""
    receipt = read_delivery_receipt(timeline, message)
    delivered = receipt.metadata.get("delivered") if receipt is not None else None
    return delivered if isinstance(delivered, bool) else None
