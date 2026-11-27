"""感知和要发出去的消息：通道和心跳之间的统一格式（D41 第 7 条）。

通道把平台消息翻译成感知交给心跳，心跳把要说的话交回给通道发出去（03 号文档“通道”一节）。
格式由心跳定义，通道依赖心跳，反过来不行。可见范围沿用时间线的定义，在这里一并导出，
通道不用直接 import 时间线。
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from aureus.timeline import VisibilityScope

__all__ = [
    "InboundQueueFullError",
    "MediaReference",
    "MessageComponent",
    "MessageKind",
    "MessageRoute",
    "MessageSink",
    "OutgoingMessage",
    "Perception",
    "QQChatAction",
    "TrustLevel",
    "VisibilityScope",
    "mentions_bot",
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


class InboundQueueFullError(Exception):
    """通道的待处理来信已达上限；本条没有入队或记入时间线。"""


@dataclass(frozen=True)
class MessageRoute:
    """收发账号与原消息地址，供通道回投与时间线追溯"""

    account_id: str
    kind: str
    recipient_id: str
    message_id: str
    temporary_group_id: str | None = None


@dataclass(frozen=True)
class MediaReference:
    """已接收消息中的平台附件标识，可按原会话查找。"""

    kind: Literal["image", "record", "video", "file"]
    file_id: str
    is_emoji: bool = False
    name: str = ""


@dataclass(frozen=True)
class MessageComponent:
    """有序的平台消息事实；来源账号、会话、参与者和消息键沿用所在的原件。"""

    kind: Literal[
        "text",
        "at",
        "reply",
        "image",
        "record",
        "video",
        "file",
        "forward",
        "card",
        "face",
        "poke",
        "reaction",
        "recall",
        "unknown",
    ]
    position: int
    text: str | None = None
    attachment_index: int | None = None
    target_account_id: str | None = None
    target_message_id: str | None = None
    all_members: bool = False
    poke_type: int | None = None
    action: str | None = None
    suffix: str | None = None
    emoji_id: str | None = None
    count: int | None = None
    forward_id: str | None = None


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
    source_message_id: str | None = None  # 通道提供的稳定入站消息键，用于语言学习去重。
    attachments: tuple[MediaReference, ...] = ()
    components: tuple[MessageComponent, ...] = ()

    @property
    def has_shared_content(self) -> bool:
        """含转发或分享卡片的来信只作解读，不作为受托写操作的授权。"""
        return any(item.kind in ("forward", "card") for item in self.components)

    def __post_init__(self) -> None:
        if not self.channel.strip():
            raise ValueError("感知必须写明来自哪个通道")
        if not self.content.strip():
            raise ValueError("感知的内容不能是空的")


def mentions_bot(perception: Perception, names: Sequence[str]) -> bool:
    """正文是否点名了它：at 段已标记，或包含身份全名/昵称（D137）。

    英文名按词边界匹配，避免 Aureus 命中 AureusBot；中文等非 ASCII 名字按子串匹配。
    名字两侧空白和空名字跳过；大小写不敏感。

    Args:
        perception: 通道解码出的感知。
        names: 当前身份的全名和昵称。

    Returns:
        提到它时为 True。
    """
    if perception.mentioned:
        return True
    content = perception.content.casefold()
    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue
        folded = name.casefold()
        if name.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])", content):
                return True
        elif folded in content:
            return True
    return False


class MessageKind(StrEnum):
    """要发出去的消息是谁说的。"""

    REPLY = "reply"  # 它自己说的话
    NOTICE = "notice"  # 系统通知，不是它说的：比如调用被拒绝、来不及回复（D35：不替它编回复）


@dataclass(frozen=True)
class QQChatAction:
    """已按会话解析的动作。成员、原件、素材由通道在实际发送前重新核对。"""

    kind: Literal["face", "at", "react", "poke", "recall", "resend_image", "send_asset"]
    target_id: str = ""
    emoji_id: str = ""
    attachment_index: int = 0
    source_message_id: str = ""


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
    emoji_digest: str | None = None
    quote_source: bool = False
    confirm_delivery: Callable[[], None] | None = None
    visibility: VisibilityScope = VisibilityScope.PRIVATE
    report_delivery_failure: Callable[[str], None] | None = None
    reply_segment: int | None = None
    can_deliver: Callable[[], bool] | None = None
    qq_action: QQChatAction | None = None
    confirm_platform_delivery: Callable[[str], None] | None = None


# 通道收消息的函数。必须很快返回、不能阻塞（心跳会直接调用它），出错只影响这个通道。
MessageSink = Callable[[OutgoingMessage], None]
