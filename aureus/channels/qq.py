"""通道 channels：把通过账号核对的 QQ 消息送入心跳"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from sqlite3 import Error as SQLiteError
from types import TracebackType
from typing import Any, Self

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import WebSocketException

from aureus.common import QQConfig
from aureus.heartbeat import (
    MediaReference,
    MessageKind,
    MessageRoute,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
)
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore

from .onebot import (
    QQ_CHANNEL_NAME,
    account_id,
    decode_message,
    encode_message,
    media_file_id,
    message_id,
)
from .onebot_transport import OneBotError, OneBotTransport, decode_media_base64
from .web import HeartbeatHandle

logger = logging.getLogger(__name__)
# 跨连接保留最近收取的消息，限制进程长期运行的占用。
_RECENT_MESSAGE_LIMIT = 16_384
_OUTGOING_LIMIT = 64
# 转码结果位于单个 WebSocket 帧，编码后需给 OneBot 的 JSON 信封留空间。
_MP3_LIMIT = 700 * 1024


def _display_name(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class QQChannel:
    """把 SnowLuma 事件翻译成感知，把心跳的回复送回原会话"""

    def __init__(
        self,
        config: QQConfig,
        token: SecretStr | None,
        heartbeat: HeartbeatHandle,
        is_owner: Callable[[str], bool],
        consume_command: Callable[[Perception], bool] | None = None,
        *,
        test_group: str | None = None,
        inbox_store: InboxStore | None = None,
        on_inbox_message: Callable[[InboxMessage], None] | None = None,
        read_emoji: Callable[[str], bytes | None] | None = None,
        record_emoji_use: Callable[[str], None] | None = None,
        allows_emoji_delivery: Callable[[], bool] | None = None,
    ) -> None:
        self._test_group = test_group
        self._config = config
        self._heartbeat = heartbeat
        self._is_owner = is_owner
        self._consume_command = consume_command
        self._inbox_store = inbox_store
        self._on_inbox_message = on_inbox_message
        self._read_emoji = read_emoji
        self._record_emoji_use = record_emoji_use
        self._allows_emoji_delivery = allows_emoji_delivery
        self._transport = OneBotTransport(
            config, token.get_secret_value() if token else None, self._receive
        )
        self._seen: OrderedDict[tuple[ConversationKey, str], None] = OrderedDict()
        self._sending: set[asyncio.Task[None]] = set()
        self._last_send: dict[ConversationKey, asyncio.Task[None]] = {}
        self._connection: asyncio.Task[None] | None = None
        self._group_lookups: dict[str, asyncio.Task[None]] = {}
        self._group_names: dict[str, str] = {}

    async def __aenter__(self) -> Self:
        self._connection = asyncio.create_task(self._transport.run(), name="aureus-onebot")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        tasks = [*self._sending, *self._group_lookups.values()]
        if self._connection is not None:
            tasks.append(self._connection)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _allows_route(self, route: MessageRoute | ConversationKey) -> bool:
        group = self._config.allowed_group_id
        if self._test_group is not None and (
            route.kind != "group" or route.recipient_id != self._test_group
        ):
            return False
        if group is None:
            return True
        if route.account_id != self._config.account_id or route.temporary_group_id is not None:
            return False
        if route.kind == "group":
            return route.recipient_id == group
        return route.kind == "private" and self._is_owner(route.recipient_id)

    def _receive(self, payload: dict[str, Any]) -> None:
        perception = decode_message(payload, self._config.account_id)
        if perception is None or perception.route is None:
            return
        route = perception.route
        # 临时来源可能没有有效群号；解码为普通回址前的原始标志也要核对。
        if (
            self._config.allowed_group_id is not None
            and route.kind == "private"
            and (
                payload.get("sub_type") not in (None, "friend", "normal")
                or payload.get("temp_source") not in (None, -1)
                or payload.get("group_id") is not None
            )
        ):
            return
        if not self._allows_route(route):
            return
        key = ConversationKey.from_perception(perception), route.message_id
        if key in self._seen:
            return
        if not (self._consume_command and self._consume_command(perception)):
            if (
                route.kind == "private"
                and route.temporary_group_id is None
                and self._is_owner(perception.sender_id)
            ):
                perception = replace(
                    perception, trust=TrustLevel.VERIFIED_USER, visibility=VisibilityScope.USER_ONLY
                )
            if self._inbox_store is not None:
                if not self._save_inbox(perception, route, payload):
                    return
            elif not self._heartbeat.submit(perception):
                return
        self._seen[key] = None
        if len(self._seen) > _RECENT_MESSAGE_LIMIT:
            self._seen.popitem(last=False)

    def allows_inbox_perception(self, perception: Perception) -> bool:
        """恢复未读时重新检查接收账号和当前允许的 QQ 范围。"""
        return perception.route is not None and self.allows_inbox_conversation(
            ConversationKey.from_perception(perception)
        )

    def allows_inbox_conversation(self, conversation: ConversationKey) -> bool:
        """先核对收件账号和接收范围，再读取该会话的未读页。"""
        return (
            conversation.platform == QQ_CHANNEL_NAME
            and conversation.account_id == self._config.account_id
            and self._allows_route(conversation)
        )

    def can_quote_reply(self, route: MessageRoute) -> bool:
        """只允许已连接账号向原群聊或普通私聊显式引用。"""
        return (
            self._transport.socket is not None
            and route.account_id == self._config.account_id
            and route.kind in ("group", "private")
            and route.temporary_group_id is None
            and account_id(route.recipient_id) is not None
            and message_id(route.message_id) is not None
            and self._allows_route(route)
        )

    async def read_inbox_attachment(
        self, perception: Perception, attachment: MediaReference
    ) -> tuple[bytes, str] | None:
        """以当前已验证连接取回获准会话里的附件字节和媒体类型。"""
        socket = self._transport.socket
        file_id = media_file_id(attachment.file_id)
        if (
            socket is None
            or file_id is None
            or attachment not in perception.attachments
            or not self.allows_inbox_perception(perception)
        ):
            return None
        try:
            if attachment.kind == "image":
                data = await self._transport.download_image(socket, file_id)
                if mime := _image_mime(data):
                    return data, mime
            else:
                response = await self._transport.call(
                    socket, "get_record", {"file_id": file_id, "out_format": "mp3"}
                )
                data = decode_media_base64(response.get("base64"), _MP3_LIMIT)
                if data and (
                    data.startswith(b"ID3")
                    or (len(data) >= 4 and data[0] == 255 and data[1] >= 224)
                ):
                    return data, "audio/mpeg"
        except (OSError, TimeoutError, WebSocketException, OneBotError) as error:
            logger.debug("QQ 附件暂不可用（%s）", type(error).__name__)
        return None

    def _save_inbox(
        self, perception: Perception, route: MessageRoute, payload: dict[str, Any]
    ) -> bool:
        assert self._inbox_store is not None
        sender = payload.get("sender")
        nickname = _display_name(sender.get("nickname")) if isinstance(sender, dict) else ""
        card = _display_name(sender.get("card")) if isinstance(sender, dict) else ""
        group = route.kind == "group"
        group_id = route.recipient_id
        name = _display_name(payload.get("group_name")) if group else nickname
        if group and name:
            self._group_names[group_id] = name
        try:
            message = self._inbox_store.add(
                perception,
                sender_name=(card or nickname) if group else nickname,
                conversation_name=(name or self._group_names.get(group_id, "")) if group else name,
            )
        except SQLiteError:
            logger.exception("QQ 收件箱写入失败")
            return False
        if message is not None:
            if self._on_inbox_message is not None:
                self._on_inbox_message(message)
            socket = self._transport.socket
            if (
                group
                and socket is not None
                and group_id not in self._group_names
                and (group_id not in self._group_lookups)
            ):
                self._group_lookups[group_id] = asyncio.create_task(
                    self._lookup_group_name(socket, message.conversation),
                    name="aureus-qq-group-name",
                )
        return True

    async def _lookup_group_name(self, socket: ClientConnection, key: ConversationKey) -> None:
        group_id = key.recipient_id
        try:
            data = await self._transport.call(socket, "get_group_info", {"group_id": group_id})
            name = _display_name(data.get("group_name"))
            if name and group_id not in self._group_names:
                assert self._inbox_store is not None
                self._inbox_store.rename(key, name)
                self._group_names[group_id] = name
        except (OSError, TimeoutError, WebSocketException, OneBotError, SQLiteError):
            logger.debug("QQ 群名暂不可用")
        finally:
            self._group_lookups.pop(group_id, None)

    def show(self, message: OutgoingMessage) -> None:
        """把已通过心跳检查的回复交给后台发送，避免网络阻塞心跳"""
        route = message.route
        if message.kind is not MessageKind.REPLY:
            return
        if route is None:
            if self._test_group is not None or self._config.allowed_group_id is not None:
                raise OneBotError("QQ 范围内的消息需要明确目的地址")
            return
        if not self._allows_route(route):
            raise OneBotError("QQ 目的地超出允许范围")
        if message.quote_source and not self.can_quote_reply(route):
            raise OneBotError("此 QQ 回复路由不支持引用")
        if message.emoji_digest is not None and not self._allows_emoji(message, route):
            raise OneBotError("QQ 表情目的地或当前权限不允许发送")
        socket = self._transport.socket
        if socket is None or len(self._sending) >= _OUTGOING_LIMIT:
            raise OneBotError("QQ 连接未就绪或发送队列已满")
        conversation = ConversationKey(
            message.channel,
            route.account_id,
            route.kind,
            route.recipient_id,
            route.temporary_group_id,
        )
        task = asyncio.create_task(
            self._send(socket, message, route, self._last_send.get(conversation)),
            name="aureus-onebot-send",
        )
        self._last_send[conversation] = task
        self._sending.add(task)

        def drop(finished: asyncio.Task[None]) -> None:
            self._sending.discard(finished)
            if self._last_send.get(conversation) is finished:
                del self._last_send[conversation]

        task.add_done_callback(drop)

    def _allows_emoji(self, message: OutgoingMessage, route: MessageRoute) -> bool:
        return (
            message.channel == QQ_CHANNEL_NAME
            and route.account_id == self._config.account_id
            and route.temporary_group_id is None
            and self._allows_route(route)
            and self._allows_emoji_delivery is not None
            and self._allows_emoji_delivery()
        )

    async def _send(
        self,
        socket: ClientConnection,
        message: OutgoingMessage,
        route: MessageRoute,
        previous: asyncio.Task[None] | None,
    ) -> None:
        try:
            if previous is not None:
                await previous
            if self._transport.socket is not socket or not self._allows_route(route):
                return
            image = None
            digest = message.emoji_digest
            if digest is not None:
                if self._read_emoji is None or not self._allows_emoji(message, route):
                    return
                image = await asyncio.to_thread(self._read_emoji, digest)
                if (
                    image is None
                    or self._transport.socket is not socket
                    or not self._allows_emoji(message, route)
                ):
                    return
            params = encode_message(message, route, image=image)
            receipt = await self._transport.call(socket, "send_msg", params)
            await self._save_delivered(message, route, receipt.get("message_id"))
            if message.confirm_delivery is not None:
                message.confirm_delivery()
            if digest is not None and self._record_emoji_use is not None:
                await asyncio.to_thread(self._record_emoji_use, digest)
        except (OSError, TimeoutError, WebSocketException, OneBotError, SQLiteError) as error:
            logger.warning("QQ 发送失败（%s）", type(error).__name__)

    async def _save_delivered(
        self, message: OutgoingMessage, route: MessageRoute, receipt_id: object
    ) -> None:
        if self._inbox_store is None:
            return
        identifier = message_id(receipt_id)
        if identifier is None:
            logger.warning("QQ 发送回执缺少有效消息键，无法归档外发消息")
            return
        perception = Perception(
            message.channel,
            route.account_id,
            "[表情]" if message.emoji_digest is not None else message.content,
            message.visibility,
            TrustLevel.VERIFIED_CONTACT,
            route=replace(route, message_id=identifier),
            quoted_message_id=route.message_id if message.quote_source else None,
        )
        try:
            await asyncio.to_thread(
                self._inbox_store.add_delivered, perception, sender_name=message.sender_name
            )
        except SQLiteError:
            logger.exception("QQ 消息已发送，但外发历史写入失败")
