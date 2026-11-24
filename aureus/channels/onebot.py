"""通道 channels：SnowLuma 正向 OneBot 的消息编解码。

协议参考 snowluma-adapter 1.1.8（GPL-v3.0-or-later）；对应路径见 THIRD_PARTY.md。
"""

import re
from base64 import b64encode
from typing import Any

from aureus.heartbeat import (
    MediaReference,
    MessageRoute,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
)

QQ_CHANNEL_NAME = "qq"
_ACCOUNT_ID = re.compile(r"[1-9][0-9]{0,19}")
_MESSAGE_ID = re.compile(r"-?[1-9][0-9]{0,19}")
# SnowLuma 的动作也接受 URL 和路径，因此只传缓存标识。
_MEDIA_ID = re.compile(r"[A-Za-z0-9_{}@+=-][A-Za-z0-9_{}@.+=-]{0,255}")


def account_id(value: object) -> str | None:
    """把 OneBot 数字账号规范为人物档案使用的字符串"""
    return _read_id(value, _ACCOUNT_ID)


def message_id(value: object) -> str | None:
    """规范 OneBot 事件或发送回执中的消息键。"""
    return _read_id(value, _MESSAGE_ID)


def media_file_id(value: object) -> str | None:
    """只接受可作为 SnowLuma 缓存标识的值。"""
    return value if isinstance(value, str) and _MEDIA_ID.fullmatch(value) else None


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
    identifier = message_id(payload.get("message_id"))
    kind = payload.get("message_type")
    group_id = account_id(payload.get("group_id"))
    if not sender_id or sender_id == receiving_account or not identifier:
        return None
    if kind == "group" and group_id:
        route = MessageRoute(receiving_account, "group", group_id, identifier)
        visibility = VisibilityScope.GROUP
    elif kind == "private":
        if payload.get("sub_type") == "group" and group_id is None:
            return None
        route = MessageRoute(receiving_account, "private", sender_id, identifier, group_id)
        visibility = VisibilityScope.PRIVATE
    else:
        return None
    text, mentioned, quoted, attachments = _read_segments(payload.get("message"), receiving_account)
    if not text:
        if not attachments:
            return None
        text = " ".join("[图片]" if item.kind == "image" else "[语音]" for item in attachments)
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
        source_message_id=f"{receiving_account}:{identifier}",
        attachments=attachments,
    )


def _read_segments(
    message: object, receiving_account: str
) -> tuple[str, bool, str | None, tuple[MediaReference, ...]]:
    if isinstance(message, str):
        return message.strip(), False, None, ()
    if not isinstance(message, list):
        return "", False, None, ()
    parts: list[str] = []
    attachments: list[MediaReference] = []
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
            case "image" | "record" as kind:
                if file_id := media_file_id(data.get("file")):
                    subtype = data.get("sub_type", data.get("subType"))
                    summary = data.get("summary")
                    is_emoji = kind == "image" and (
                        (str(subtype).isdigit() and str(subtype).lstrip("0") not in {"", "4", "9"})
                        or (isinstance(summary, str) and summary.strip() not in {"", "[图片]"})
                    )
                    attachments.append(MediaReference(kind, file_id, is_emoji))
    return "".join(parts).strip(), mentioned, quoted, tuple(attachments)


def encode_message(
    message: OutgoingMessage, route: MessageRoute, *, image: bytes | None = None
) -> dict[str, Any]:
    """按 SnowLuma 的 send_msg 编码；仅按显式请求引用原消息。"""
    part = (
        {
            "type": "image",
            "data": {"file": "base64://" + b64encode(image).decode("ascii"), "sub_type": 1},
        }
        if image is not None
        else {"type": "text", "data": {"text": message.content}}
    )
    segments = [part]
    if message.quote_source:
        segments.insert(0, {"type": "reply", "data": {"id": route.message_id}})
    params: dict[str, Any] = {"message_type": route.kind, "message": segments}
    if route.kind == "group":
        params["group_id"] = route.recipient_id
    else:
        params["user_id"] = route.recipient_id
    return params
