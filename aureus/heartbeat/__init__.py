"""心跳子系统 heartbeat：常驻主循环，以及它和通道之间的消息格式（D41）。

通道依赖本包，本包不依赖通道。本包只拿到一个能调用器官的对象（核心组装时传安全网关）。
"""

from .downtime import Downtime, assess_downtime
from .events import (
    EVENT_COGNITIVE_STATE,
    EVENT_EMERGENCY_RESUME,
    EVENT_EMERGENCY_STOP,
    EVENT_IDENTITY_CHANGED,
    EVENT_MESSAGE,
    EVENT_ORGAN_CALL_ABANDONED,
    EVENT_REPLY_DECLINED,
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
from .planning import Plan, ReplyLength, parse_plan
from .reply_strategy import ReplyStrategy, choose_strategy

__all__ = [
    "EVENT_COGNITIVE_STATE",
    "EVENT_EMERGENCY_RESUME",
    "EVENT_EMERGENCY_STOP",
    "EVENT_IDENTITY_CHANGED",
    "EVENT_MESSAGE",
    "EVENT_ORGAN_CALL_ABANDONED",
    "EVENT_REPLY_DECLINED",
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
    "Plan",
    "ReplyLength",
    "ReplyStrategy",
    "TrustLevel",
    "VisibilityScope",
    "assess_downtime",
    "choose_strategy",
    "parse_plan",
]
