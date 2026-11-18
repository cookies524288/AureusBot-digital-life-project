"""心跳子系统 heartbeat：常驻主循环，以及它和通道之间的消息格式（D41）。

通道依赖本包，本包不依赖通道。本包只拿到一个能调用器官的对象（核心组装时传安全网关）。
"""

from .downtime import Downtime, assess_downtime
from .events import (
    EVENT_COGNITIVE_STATE,
    EVENT_EMERGENCY_RESUME,
    EVENT_EMERGENCY_STOP,
    EVENT_MESSAGE,
    EVENT_ORGAN_CALL_ABANDONED,
    EVENT_SELF_CHECK,
    EVENT_SHUT_DOWN,
    EVENT_WOKE_UP,
)
from .main_loop import Heartbeat, OrganCaller
from .perception import (
    MessageKind,
    MessageSink,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
)

__all__ = [
    "EVENT_COGNITIVE_STATE",
    "EVENT_EMERGENCY_RESUME",
    "EVENT_EMERGENCY_STOP",
    "EVENT_MESSAGE",
    "EVENT_ORGAN_CALL_ABANDONED",
    "EVENT_SELF_CHECK",
    "EVENT_SHUT_DOWN",
    "EVENT_WOKE_UP",
    "Downtime",
    "Heartbeat",
    "MessageKind",
    "MessageSink",
    "OrganCaller",
    "OutgoingMessage",
    "Perception",
    "TrustLevel",
    "VisibilityScope",
    "assess_downtime",
]
