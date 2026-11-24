"""通道 channels：SnowLuma 正向 OneBot 的消息编解码。

协议参考 snowluma-adapter 1.1.8（GPL-v3.0-or-later）；对应路径见 THIRD_PARTY.md。
"""

import re
from base64 import b64encode
from typing import Any
from uuid import uuid4

from aureus.heartbeat import (
    MediaReference,
    MessageComponent,
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
    text, mentioned, quoted, attachments, components = read_message_segments(
        payload.get("message"), receiving_account
    )
    if not text:
        if attachments:
            text = " ".join("[图片]" if item.kind == "image" else "[语音]" for item in attachments)
        elif any(item.kind == "poke" for item in components):
            text = "[窗口抖动]"
        elif quoted is not None:
            text = "[引用消息]"
        else:
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
        source_message_id=f"{receiving_account}:{identifier}",
        attachments=attachments,
        components=components,
    )


def decode_poke_notice(payload: dict[str, Any], receiving_account: str) -> Perception | None:
    """按 SnowLuma 的 notify/poke 事件区分群戳和普通私聊戳，不构造可引用消息。"""
    if (
        payload.get("post_type") != "notice"
        or payload.get("notice_type") != "notify"
        or payload.get("sub_type") != "poke"
        or account_id(payload.get("self_id")) != receiving_account
    ):
        return None
    target = account_id(payload.get("target_id"))
    if "group_id" in payload:
        group = account_id(payload["group_id"])
        if group is None:
            return None
        actor = account_id(payload.get("user_id"))
        kind, recipient, visibility = "group", group, VisibilityScope.GROUP
    else:
        peer = account_id(payload.get("user_id"))
        actor = account_id(payload.get("sender_id"))
        if (
            peer is None
            or peer == receiving_account
            or {actor, target} != {receiving_account, peer}
        ):
            return None
        kind, recipient, visibility = "private", peer, VisibilityScope.PRIVATE
    timestamp = payload.get("time")
    return Perception(
        QQ_CHANNEL_NAME,
        actor or "",
        "[戳一戳]",
        visibility,
        TrustLevel.UNVERIFIED,
        route=MessageRoute(receiving_account, kind, recipient, f"poke:{uuid4().hex}"),
        source_timestamp=timestamp if type(timestamp) is int else None,
        components=(
            MessageComponent(
                "poke",
                0,
                target_account_id=target,
                action=_notice_text(payload.get("action")),
                suffix=_notice_text(payload.get("suffix")),
            ),
        ),
    )


def _notice_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def read_message_segments(
    message: object, receiving_account: str
) -> tuple[str, bool, str | None, tuple[MediaReference, ...], tuple[MessageComponent, ...]]:
    """提取原始数组的顺序与事实，同时保留心跳使用的正文兼容格式。"""
    if isinstance(message, str):
        return message.strip(), False, None, (), (MessageComponent("text", 0, text=message),)
    if not isinstance(message, list):
        return "", False, None, (), ()
    parts: list[str] = []
    attachments: list[MediaReference] = []
    components: list[MessageComponent] = []
    mentioned = False
    quoted = None
    for position, segment in enumerate(message):
        if not isinstance(segment, dict) or not isinstance(data := segment.get("data"), dict):
            components.append(MessageComponent("unknown", position))
            continue
        match segment.get("type"):
            case "text" if isinstance(text := data.get("text"), str):
                parts.append(text)
                components.append(MessageComponent("text", position, text=text))
            case "at":
                all_members = data.get("qq") == "all"
                target = account_id(data.get("qq"))
                if all_members:
                    parts.append("@全体成员 ")
                elif target is not None:
                    mentioned |= target == receiving_account
                    parts.append(f"@{target} ")
                components.append(
                    MessageComponent(
                        "at", position, target_account_id=target, all_members=all_members
                    )
                )
            case "reply":
                target_message = message_id(data.get("id"))
                quoted = quoted or target_message
                components.append(
                    MessageComponent("reply", position, target_message_id=target_message)
                )
            case "image" | "record" as kind:
                index = None
                if file_id := media_file_id(data.get("file")):
                    subtype = data.get("sub_type", data.get("subType"))
                    summary = data.get("summary")
                    is_emoji = kind == "image" and (
                        (str(subtype).isdigit() and str(subtype).lstrip("0") not in {"", "4", "9"})
                        or (isinstance(summary, str) and summary.strip() not in {"", "[图片]"})
                    )
                    index = len(attachments)
                    attachments.append(MediaReference(kind, file_id, is_emoji))
                components.append(MessageComponent(kind, position, attachment_index=index))
            case "poke":
                poke_type = data.get("type")
                components.append(
                    MessageComponent(
                        "poke", position, poke_type=poke_type if type(poke_type) is int else None
                    )
                )
            case _:
                components.append(MessageComponent("unknown", position))
    return "".join(parts).strip(), mentioned, quoted, tuple(attachments), tuple(components)


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
