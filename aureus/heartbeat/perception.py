"""感知和要发出去的消息：通道和心跳之间的统一格式（D41 第 7 条）。

通道把平台消息翻译成感知交给心跳，心跳把要说的话交回给通道发出去（03 号文档“通道”一节）。
格式由心跳定义，通道依赖心跳，反过来不行。可见范围沿用时间线的定义，在这里一并导出，
通道不用直接 import 时间线。
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aureus.timeline import VisibilityScope

__all__ = [
    "MessageKind",
    "MessageRoute",
    "MessageSink",
    "OutgoingMessage",
    "Perception",
    "TrustLevel",
    "VisibilityScope",
]


class TrustLevel(StrEnum):
    """通道对“发消息的是谁”有多确定（03 号文档：每条感知都带可信度）。"""

    # 通道能确认是用户本人：本机的终端；以后登录过的网页。
    VERIFIED_USER = "verified_user"
    # 通道能确认这是某个具体的账号，而且不是用户：可以回复，按这个账号的档案认人（1.8）。
    # Phase 1 里只有测试的模拟通道会给这个等级；真实通道放开留到 Phase 3（D53 第 7 条）。
    VERIFIED_CONTACT = "verified_contact"
    # 只知道平台账号，没法确认是谁（以后的 QQ 等平台）：只记录，不回复。
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class MessageRoute:
    """收发账号与原消息地址，供通道回投与时间线追溯"""

    account_id: str
    kind: str
    recipient_id: str
    message_id: str
    temporary_group_id: str | None = None


@dataclass(frozen=True)
class Perception:
    """通道交给心跳的一条感知。"""

    channel: str  # 通道名，比如 terminal；回复会发回这个通道
    sender_id: str  # 发消息的人在这个平台上的账号 ID
    content: str
    visibility: VisibilityScope
    trust: TrustLevel
    route: MessageRoute | None = None
    mentioned: bool = False
    quoted_message_id: str | None = None
    source_timestamp: int | None = None

    def __post_init__(self) -> None:
        if not self.channel.strip():
            raise ValueError("感知必须写明来自哪个通道")
        if not self.content.strip():
            raise ValueError("感知的内容不能是空的")


class MessageKind(StrEnum):
    """要发出去的消息是谁说的。"""

    REPLY = "reply"  # 它自己说的话
    NOTICE = "notice"  # 系统通知，不是它说的：比如调用被拒绝、来不及回复（D35：不替它编回复）


@dataclass(frozen=True)
class OutgoingMessage:
    """心跳交给通道发出去的一条消息。"""

    channel: str
    recipient_id: str
    content: str
    kind: MessageKind
    sender_name: str  # 显示的名字：回复是它的名字，通知是“系统”
    timeline_id: int | None = None  # 同一历史回复拆出的各段共享序号
    timestamp: datetime | None = None
    route: MessageRoute | None = None


# 通道收消息的函数。必须很快返回、不能阻塞（心跳会直接调用它），出错只影响这个通道。
MessageSink = Callable[[OutgoingMessage], None]
