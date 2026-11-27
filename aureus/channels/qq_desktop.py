"""通过已核对账号的连接，执行桌面明确选择的 SnowLuma 操作。"""

from base64 import b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import WebSocketException

from aureus.heartbeat import MessageKind, MessageRoute, OutgoingMessage, VisibilityScope
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage

from .onebot import account_id, encode_message, message_id
from .onebot_transport import OneBotError, OneBotTransport, OneBotUnsupportedError


@dataclass(frozen=True)
class DesktopError(Exception):
    """可以显示给维护者的错误；不透传连接端的内容。"""

    status: int
    detail: str


class QQDesktop:
    """不接受调用方自选的 OneBot action、消息 ID 或成员 ID。"""

    def __init__(
        self,
        transport: OneBotTransport,
        allows: Callable[[ConversationKey], bool],
        save_delivered: Callable[[OutgoingMessage, MessageRoute, object, object], Awaitable[None]],
    ) -> None:
        self._transport = transport
        self._allows = allows
        self._save_delivered = save_delivered

    def check_scope(self, key: ConversationKey) -> None:
        """核对普通私聊或群聊仍在当前部署允许的范围。"""
        if (
            key.temporary_group_id is not None
            or key.kind not in {"private", "group"}
            or account_id(key.recipient_id) is None
            or not self._allows(key)
        ):
            raise DesktopError(403, "此会话不在当前 QQ 操作范围内；临时私聊不支持桌面外发。")

    def ready(self, key: ConversationKey) -> ClientConnection:
        """取得与当前收发账号核对通过的连接。"""
        self.check_scope(key)
        socket = self._transport.socket
        if socket is None:
            raise DesktopError(503, "QQ 尚未连接，没有发送。")
        return socket

    def _recheck(
        self, socket: ClientConnection, key: ConversationKey, authorize: Callable[[], None]
    ) -> None:
        authorize()
        if self.ready(key) is not socket:
            raise DesktopError(503, "QQ 连接已切换，没有发送。")

    async def send_text(
        self,
        key: ConversationKey,
        text: str,
        quote: InboxMessage | None,
        mention: InboxMessage | None,
        authorize: Callable[[], None],
    ) -> str:
        """发送文字和可选的单条显式引用、单个已核定群成员提及。"""
        socket = self.ready(key)
        authorize()
        quote_id = await self._quote(socket, key, quote) if quote is not None else None
        target = await self._member(socket, key, mention) if mention is not None else None
        route = MessageRoute(key.account_id, key.kind, key.recipient_id, quote_id or "")
        outgoing = OutgoingMessage(
            key.platform,
            key.recipient_id,
            text,
            MessageKind.REPLY,
            key.account_id,
            route=route,
            quote_source=quote_id is not None,
            visibility=_visibility(key),
        )
        params = encode_message(outgoing, route)
        if target is not None:
            params["message"].insert(
                int(quote_id is not None), {"type": "at", "data": {"qq": target}}
            )
        self._recheck(socket, key, authorize)
        receipt = await self._call(socket, "send_msg", params, "发送文字")
        identifier = message_id(receipt.get("message_id"))
        if identifier is None:
            raise DesktopError(502, "QQ 接受了请求，但没有返回有效消息编号；请勿直接重试。")
        await self._save_delivered(outgoing, route, identifier, params["message"])
        return identifier

    async def send_poke(
        self, key: ConversationKey, member: InboxMessage | None, authorize: Callable[[], None]
    ) -> None:
        """发送戳一戳。成功响应无消息 ID，个性化文案只由实际通知提供。"""
        socket = self.ready(key)
        authorize()
        if key.kind == "group":
            target = await self._member(socket, key, member)
            params = {"group_id": key.recipient_id, "user_id": target}
        elif member is None and key.account_id != key.recipient_id:
            params = {"user_id": key.recipient_id}
        else:
            raise DesktopError(422, "普通私聊只能戳当前对方。")
        self._recheck(socket, key, authorize)
        await self._call(socket, "send_poke", params, "戳一戳")

    async def upload_file(
        self, key: ConversationKey, name: str, content: bytes, authorize: Callable[[], None]
    ) -> str:
        """仅使用调用方已验证的字节；群与私聊的回执都是文件 ID，而非消息 ID。"""
        socket = self.ready(key)
        if key.kind == "group":
            action, recipient = "upload_group_file", "group_id"
        else:
            action, recipient = "upload_private_file", "user_id"
        params = {
            recipient: key.recipient_id,
            "name": name,
            "file": "base64://" + b64encode(content).decode("ascii"),
        }
        self._recheck(socket, key, authorize)
        receipt = await self._call(socket, action, params, "上传文件")
        identifier = receipt.get("file_id")
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 512:
            raise DesktopError(502, "QQ 未返回有效文件编号，请先检查会话或群文件，不要直接重试。")
        return identifier

    async def avatar_source(
        self,
        socket: ClientConnection,
        row: InboxMessage,
        sender: str,
        authorized: Callable[[], bool],
    ) -> str | None:
        """仅取固定版装扮查询实际返回、且归属正确的 avatar_url。"""
        key = row.conversation
        if not authorized() or self.ready(key) is not socket:
            return None
        if key.kind == "group" and sender != key.account_id:
            await self._member(socket, key, row)
        if not authorized() or self.ready(key) is not socket:
            return None
        data = await self._call(
            socket,
            "_get_friend_dress",
            {"user_id": sender},
            "读取 QQ 头像资料",
            preflight=True,
        )
        url = data.get("avatar_url")
        if account_id(data.get("target_uin")) != sender or not isinstance(url, str):
            return None
        return url

    async def _member(
        self, socket: ClientConnection, key: ConversationKey, message: InboxMessage | None
    ) -> str:
        target = account_id(message.perception.sender_id) if message is not None else None
        if (
            key.kind != "group"
            or message is None
            or message.conversation != key
            or message.direction not in {"incoming", "archived"}
            or target is None
            or target == key.account_id
        ):
            raise DesktopError(422, "只能从当前群聊的消息中选择成员。")
        return await self.verify_member(socket, key, target)

    async def verify_member(
        self, socket: ClientConnection, key: ConversationKey, target: str
    ) -> str:
        """复核已从本会话原件或戳通知选出的账号是否仍是本群成员。"""
        if key.kind != "group" or account_id(target) is None or target == key.account_id:
            raise DesktopError(422, "不是当前群可选的成员。")
        data = await self._call(
            socket,
            "get_group_member_info",
            {"group_id": key.recipient_id, "user_id": target, "no_cache": True},
            "核对群成员",
            preflight=True,
        )
        nickname = data.get("nickname")
        if (
            account_id(data.get("group_id")) != key.recipient_id
            or account_id(data.get("user_id")) != target
            or not isinstance(nickname, str)
            or not nickname.strip()
        ):
            raise DesktopError(409, "QQ 没有确认对方仍是当前群成员，没有发送。")
        return target

    async def _quote(
        self, socket: ClientConnection, key: ConversationKey, item: InboxMessage
    ) -> str:
        return await self.verify_message(socket, key, item)

    async def verify_message(
        self, socket: ClientConnection, key: ConversationKey, item: InboxMessage
    ) -> str:
        """按原会话、原作者及编号复核消息，不能以另一会话同号原件代替。"""
        data = await self.verified_message_data(socket, key, item)
        return str(data["message_id"])

    async def verified_message_data(
        self, socket: ClientConnection, key: ConversationKey, item: InboxMessage
    ) -> dict[str, Any]:
        """返回已校验会话、作者和编号的平台原件；调用方只读取指定位置的附件。"""
        route = item.perception.route
        identifier = message_id(route.message_id) if route else None
        if item.conversation != key or identifier is None or item.direction == "notice":
            raise DesktopError(422, "只能引用当前会话内有平台编号的消息。")
        data = await self._call(
            socket, "get_msg", {"message_id": identifier}, "核对引用原文", preflight=True
        )
        sender = account_id(data.get("user_id"))
        if key.kind == "group":
            matches = account_id(data.get("group_id")) == key.recipient_id
        else:
            peer = account_id(data.get("target_id")) if sender == key.account_id else sender
            matches = data.get("sub_type") == "friend" and peer == key.recipient_id
        if (
            not matches
            or data.get("message_type") != key.kind
            or sender != item.perception.sender_id
            or message_id(data.get("message_id")) != identifier
        ):
            raise DesktopError(409, "QQ 未确认这条原文属于当前会话，没有发送。")
        return data

    async def _call(
        self,
        socket: ClientConnection,
        action: str,
        params: dict[str, Any],
        label: str,
        *,
        preflight: bool = False,
    ) -> dict[str, object]:
        try:
            return await self._transport.call(socket, action, params)
        except OneBotUnsupportedError:
            raise DesktopError(501, f"当前 QQ 连接不支持{label}。") from None
        except (OSError, TimeoutError, WebSocketException, OneBotError):
            detail = "没有发送。" if preflight else "未确认是否成功，请先检查 QQ，不要直接重试。"
            raise DesktopError(502, f"QQ 未能完成{label}，{detail}") from None


def _visibility(key: ConversationKey) -> VisibilityScope:
    return VisibilityScope.GROUP if key.kind == "group" else VisibilityScope.USER_ONLY
