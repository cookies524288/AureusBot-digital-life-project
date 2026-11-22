"""通道 channels：SnowLuma 正向 OneBot 的消息编解码。

协议参考 snowluma-adapter 1.1.8（GPL-v3.0-or-later）；对应路径见 THIRD_PARTY.md。
"""

import re
from typing import Any

from aureus.heartbeat import MessageRoute, OutgoingMessage, Perception, TrustLevel, VisibilityScope

QQ_CHANNEL_NAME = "qq"
_ACCOUNT_ID = re.compile(r"[1-9][0-9]{0,19}")
_MESSAGE_ID = re.compile(r"-?[1-9][0-9]{0,19}")


def account_id(value: object) -> str | None:
    """把 OneBot 数字账号规范为人物档案使用的字符串"""
    return _read_id(value, _ACCOUNT_ID)


def _read_id(value: object, pattern: re.Pattern[str]) -> str | None:
    if type(value) not in (int, str):
        return None
    result = str(value)
    return result if pattern.fullmatch(result) else None


def decode_message(payload: dict[str, Any], receiving_account: str) -> Perception | None:
    """从 OneBot 消息事件提取正文、发言者和回投地址"""
    if (
        payload.get("post_type") != "message"
        or account_id(payload.get("self_id")) != receiving_account
    ):
        return None
    sender = payload.get("sender")
    sender_id = account_id(payload.get("user_id"))
    if sender_id is None and "user_id" not in payload and isinstance(sender, dict):
        sender_id = account_id(sender.get("user_id"))
    message_id = _read_id(payload.get("message_id"), _MESSAGE_ID)
    kind = payload.get("message_type")
    group_id = account_id(payload.get("group_id"))
    if not sender_id or sender_id == receiving_account or not message_id:
        return None
    if kind == "group" and group_id:
        route = MessageRoute(receiving_account, "group", group_id, message_id)
        visibility = VisibilityScope.GROUP
    elif kind == "private":
        if payload.get("sub_type") == "group" and group_id is None:
            return None
        route = MessageRoute(receiving_account, "private", sender_id, message_id, group_id)
        visibility = VisibilityScope.PRIVATE
    else:
        return None
    text, mentioned, quoted = _read_segments(payload.get("message"), receiving_account)
    if not text:
        return None
    timestamp = payload.get("time")
    return Perception(
        QQ_CHANNEL_NAME,
        sender_id,
        text,
        visibility,
        TrustLevel.VERIFIED_CONTACT,
        route=route,
        mentioned=mentioned,
        quoted_message_id=quoted,
        source_timestamp=timestamp if type(timestamp) is int else None,
        source_message_id=f"{receiving_account}:{message_id}",
    )


def _read_segments(message: object, receiving_account: str) -> tuple[str, bool, str | None]:
    if isinstance(message, str):
        return message.strip(), False, None
    if not isinstance(message, list):
        return "", False, None
    parts: list[str] = []
    mentioned = False
    quoted = None
    for segment in message:
        if not isinstance(segment, dict) or not isinstance(data := segment.get("data"), dict):
            continue
        match segment.get("type"):
            case "text" if isinstance(text := data.get("text"), str):
                parts.append(text)
            case "at":
                if data.get("qq") == "all":
                    parts.append("@全体成员 ")
                elif target := account_id(data.get("qq")):
                    mentioned |= target == receiving_account
                    parts.append(f"@{target} ")
            case "reply":
                quoted = quoted or _read_id(data.get("id"), _MESSAGE_ID)
    return "".join(parts).strip(), mentioned, quoted


def encode_message(message: OutgoingMessage, route: MessageRoute) -> dict[str, Any]:
    """按 SnowLuma 的通用 send_msg 动作编码，引用本次回复的原消息"""
    params: dict[str, Any] = {
        "message_type": route.kind,
        "message": [
            {"type": "reply", "data": {"id": route.message_id}},
            {"type": "text", "data": {"text": message.content}},
        ],
    }
    if route.kind == "group":
        params["group_id"] = route.recipient_id
    else:
        params["user_id"] = route.recipient_id
    return params
