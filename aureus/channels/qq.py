"""通道 channels：把通过账号核对的 QQ 消息送入心跳"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from sqlite3 import Error as SQLiteError
from types import TracebackType
from typing import Any, Self

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import WebSocketException

from aureus.common import Clock, QQConfig, SystemClock
from aureus.common.async_cleanup import finish_after, wait_for_cleanup
from aureus.heartbeat import (
    MediaReference,
    MessageKind,
    MessageRoute,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
    mentions_bot,
)
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore

from .onebot import (
    QQ_CHANNEL_NAME,
    account_id,
    decode_message,
    decode_poke_notice,
    decode_reaction_notice,
    decode_recall_notice,
    encode_message,
    media_file_id,
    message_id,
    read_message_segments,
)
from .onebot_transport import OneBotError, OneBotTransport, decode_media_base64
from .qq_desktop import DesktopError, QQDesktop
from .qq_rich import attachment_mime, emoji_id, face_label, platform_media_url, short_text
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
        on_social_notice: Callable[[Perception], None] | None = None,
        clock: Clock | None = None,
        read_emoji: Callable[[str], bytes | None] | None = None,
        allows_emoji_asset: Callable[[str], bool] | None = None,
        record_emoji_use: Callable[[str], None] | None = None,
        allows_emoji_delivery: Callable[[], bool] | None = None,
        bot_names: Callable[[], Sequence[str]] = lambda: (),
    ) -> None:
        self._test_group = test_group
        self._config = config
        self._heartbeat = heartbeat
        self._is_owner = is_owner
        self._consume_command = consume_command
        self._inbox_store = inbox_store
        self._on_inbox_message = on_inbox_message
        self._on_social_notice = on_social_notice
        self._clock = clock or SystemClock()
        self._read_asset: Callable[[ConversationKey, str], tuple[str, bytes] | None] | None = None
        self._allows_asset: Callable[[ConversationKey, str], bool] | None = None
        self._read_emoji = read_emoji
        self._allows_emoji_asset = allows_emoji_asset
        self._record_emoji_use = record_emoji_use
        self._allows_emoji_delivery = allows_emoji_delivery
        self._bot_names = bot_names
        self._transport = OneBotTransport(
            config, token.get_secret_value() if token else None, self._receive
        )
        self.desktop = QQDesktop(
            self._transport, self.allows_inbox_conversation, self._save_delivered
        )
        self._seen: OrderedDict[tuple[ConversationKey, str], None] = OrderedDict()
        self._recent_notices: OrderedDict[tuple[ConversationKey, str], Perception] = OrderedDict()
        self._recent_actions: OrderedDict[tuple[ConversationKey, str], None] = OrderedDict()
        self._cooldowns: OrderedDict[tuple[ConversationKey, str, str], datetime] = OrderedDict()
        self._fetching: set[asyncio.Task[None]] = set()
        self._sending: set[asyncio.Task[None]] = set()
        self._in_flight: set[asyncio.Task[None]] = set()
        self._last_send: dict[ConversationKey, asyncio.Task[None]] = {}
        self._connection: asyncio.Task[None] | None = None
        self._group_lookups: dict[str, asyncio.Task[None]] = {}
        self._group_names: dict[str, str] = {}
        self._closing = False

    async def __aenter__(self) -> Self:
        self._closing = False
        self._connection = asyncio.create_task(self._transport.run(), name="aureus-onebot")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closing = True
        try:
            await wait_for_cleanup(asyncio.create_task(self._drain_sends(), name="aureus-qq-close"))
        except BaseException as close_error:
            if exc_value is not None:
                raise BaseExceptionGroup("运行和 QQ 收尾均出错", [exc_value, close_error]) from None
            raise

    async def _drain_sends(self) -> None:
        async with finish_after(self._disconnect):
            sending = set(self._sending)
            tasks = [*self._fetching, *self._group_lookups.values(), *(sending - self._in_flight)]
            for task in tasks:
                task.cancel()
            await self._settle(set(tasks) | sending)

    async def _disconnect(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.cancel()
            await self._settle({connection})

    @staticmethod
    async def _settle(tasks: set[asyncio.Task[None]]) -> None:
        if not tasks:
            return
        await asyncio.wait(tasks)
        errors = [task.exception() for task in tasks if not task.cancelled()]
        failures = [error for error in errors if error is not None]
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("QQ 后台任务收尾失败", failures)

    def _allows_route(self, route: MessageRoute | ConversationKey) -> bool:
        group = self._config.allowed_group_id
        if self._test_group is not None and (
            route.kind != "group" or route.recipient_id != self._test_group
        ):
            return False
        if route.account_id != self._config.account_id:
            return False
        if group is None:
            return True
        if route.temporary_group_id is not None:
            return False
        if route.kind == "group":
            return route.recipient_id == group
        return route.kind == "private" and self._is_owner(route.recipient_id)

    def _receive(self, payload: dict[str, Any]) -> None:
        notice = payload.get("post_type") == "notice"
        account = self._config.account_id
        perception = (
            (
                decode_poke_notice(payload, account)
                or decode_recall_notice(payload, account)
                or decode_reaction_notice(payload, account)
            )
            if notice
            else decode_message(payload, account)
        )
        if perception is None or perception.route is None:
            return
        route = perception.route
        # D137：at 段之外，正文提到当前身份的全名或昵称也算点名；保存前写进同一个标记。
        if not perception.mentioned and mentions_bot(perception, self._bot_names()):
            perception = replace(perception, mentioned=True)
        # 临时来源可能没有有效群号；解码为普通回址前的原始标志也要核对。
        if (
            self._config.allowed_group_id is not None
            and route.kind == "private"
            and (
                (not notice and payload.get("sub_type") not in (None, "friend", "normal"))
                or payload.get("temp_source") not in (None, -1)
                or payload.get("group_id") is not None
            )
        ):
            return
        if not self._allows_route(route):
            return
        if notice:
            self._receive_notice(perception, route, payload)
            return
        archived = not perception.attachments and not any(
            (item.kind == "text" and item.text and item.text.strip())
            or (item.kind == "at" and (item.all_members or item.target_account_id))
            or (item.kind in {"face", "forward", "card"} and item.text)
            for item in perception.components
        )
        if archived:
            if self._inbox_store is not None:
                if route.kind == "private" and self._is_owner(route.recipient_id):
                    perception = replace(perception, visibility=VisibilityScope.USER_ONLY)
                self._save_inbox(
                    perception, route, payload, archive="notice" if notice else "message"
                )
            return
        key = ConversationKey.from_perception(perception), route.message_id
        if key in self._seen:
            return
        self._seen[key] = None
        if len(self._seen) > _RECENT_MESSAGE_LIMIT:
            self._seen.popitem(last=False)
        if (
            not self._closing
            and any(
                item.kind == "forward" and item.forward_id and item.text == "[合并转发]"
                for item in perception.components
            )
            and len(self._fetching) < _OUTGOING_LIMIT
            and self._transport.socket is not None
        ):
            task = asyncio.create_task(
                self._fetch_forward(self._transport.socket, perception, route, payload)
            )
            self._fetching.add(task)
            task.add_done_callback(self._fetching.discard)
            return
        self._receive_message(perception, route, payload)

    def _receive_message(
        self, perception: Perception, route: MessageRoute, payload: dict[str, Any]
    ) -> None:
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
                self._save_inbox(perception, route, payload)
            else:
                self._heartbeat.submit(perception)

    async def _fetch_forward(
        self,
        socket: ClientConnection,
        perception: Perception,
        route: MessageRoute,
        payload: dict[str, Any],
    ) -> None:
        segments = list(payload["message"])
        count = 0
        for item in perception.components:
            if (
                item.kind == "forward"
                and item.forward_id
                and item.text == "[合并转发]"
                and count < 3
            ):
                if self._transport.socket is not socket or not self._allows_route(route):
                    return
                count += 1
                try:
                    data = await self._transport.call(
                        socket, "get_forward_msg", {"id": item.forward_id}
                    )
                    segments[item.position] = {
                        "type": "forward",
                        "data": {"id": item.forward_id, "content": data.get("messages")},
                    }
                except (OneBotError, OSError, TimeoutError, WebSocketException):
                    pass
        if self._transport.socket is socket and self._allows_route(route):
            content, _, _, _, components = read_message_segments(segments, route.account_id)
            self._receive_message(
                replace(perception, components=components, content=content), route, payload
            )

    def _cooldown(self, key: ConversationKey, actor: str, kind: str) -> bool:
        slot, now = (key, actor, kind), self._clock.now()
        last = self._cooldowns.get(slot)
        if last is not None and now - last < timedelta(seconds=self._config.poke_cooldown_seconds):
            return False
        self._cooldowns[slot] = now
        self._cooldowns.move_to_end(slot)
        if len(self._cooldowns) > _RECENT_MESSAGE_LIMIT:
            self._cooldowns.popitem(last=False)
        return True

    def _receive_notice(
        self, perception: Perception, route: MessageRoute, payload: dict[str, Any]
    ) -> None:
        key = ConversationKey.from_perception(perception)
        if route.kind == "private" and self._is_owner(route.recipient_id):
            perception = replace(perception, visibility=VisibilityScope.USER_ONLY)
        notice_key = key, route.message_id
        if notice_key in self._recent_notices:
            return
        component = perception.components[0]
        store = self._inbox_store
        target = component.target_message_id
        original = (
            store.find_platform_message(key, target) if store is not None and target else None
        )
        if component.kind == "recall" and target and store is not None:
            if original is not None and component.target_account_id not in (
                None,
                original.perception.sender_id,
            ):
                return
            if original is not None and component.target_account_id is None:
                component = replace(component, target_account_id=original.perception.sender_id)
                perception = replace(perception, components=(component,))
            store.mark_recalled(key, target)
        if store is not None and not self._save_inbox(perception, route, payload, archive="notice"):
            return
        self._recent_notices[notice_key] = perception
        if len(self._recent_notices) > _RECENT_MESSAGE_LIMIT:
            self._recent_notices.popitem(last=False)
        if self._on_social_notice is None or perception.sender_id == route.account_id:
            return
        if component.kind == "recall":
            if (
                self._config.follow_recall_enabled
                and original is not None
                and original.direction == "incoming"
            ):
                callback = self._on_social_notice
                sending = self._last_send.get(key)
                if sending is not None and not sending.done():
                    sending.add_done_callback(lambda _: callback(perception))
                else:
                    callback(perception)
        elif (
            component.kind == "poke"
            and self._config.poke_response_enabled
            and component.target_account_id == route.account_id
            and self._cooldown(key, perception.sender_id, "received")
        ):
            self._on_social_notice(
                replace(
                    perception,
                    content="对方戳了你一下。",
                    trust=TrustLevel.VERIFIED_CONTACT,
                    mentioned=True,
                )
            )
        elif (
            component.kind == "reaction"
            and original is not None
            and original.direction == "outgoing"
        ):
            detail = "、".join(
                f"{face_label(item.emoji_id)}（当前 {item.count}）"
                for item in perception.components
                if item.emoji_id is not None
            )
            verb = "取消了" if component.action == "remove" else "贴了"
            self._on_social_notice(
                replace(
                    perception,
                    content=f"对方{verb}你那条消息下的表情：{detail}",
                    trust=TrustLevel.VERIFIED_CONTACT,
                    quoted_message_id=target,
                )
            )

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
        key = ConversationKey.from_perception(perception)
        store, route = self._inbox_store, perception.route

        def current() -> bool:
            if self._transport.socket is not socket or not self.allows_inbox_perception(perception):
                return False
            row = store.find_platform_message(key, route.message_id) if store and route else None
            return (
                row is not None
                and row.perception.sender_id == perception.sender_id
                and row.perception.visibility == perception.visibility
                and attachment in row.perception.attachments
            )

        if not current():
            return None
        try:
            if attachment.kind == "image":
                data = await self._transport.download_image(socket, file_id)
                if current() and (mime := _image_mime(data)):
                    return data, mime
            elif attachment.kind == "record":
                response = await self._transport.call(
                    socket, "get_record", {"file_id": file_id, "out_format": "mp3"}
                )
                data = decode_media_base64(response.get("base64"), _MP3_LIMIT)
                if (
                    current()
                    and data
                    and (
                        data.startswith(b"ID3")
                        or (len(data) >= 4 and data[0] == 255 and data[1] >= 224)
                    )
                ):
                    return data, "audio/mpeg"
            elif store is not None and route is not None and key.temporary_group_id is None:
                source = store.find_platform_message(key, route.message_id)
                if source is None:
                    return None
                response = await self.desktop.verified_message_data(socket, key, source)
                segments = response.get("message")
                if not isinstance(segments, list):
                    return None
                matched = next(
                    (
                        segment["data"]
                        for segment in segments
                        if isinstance(segment, dict)
                        and segment.get("type") == attachment.kind
                        and isinstance(segment.get("data"), dict)
                        and file_id
                        in (
                            segment["data"].get("id"),
                            segment["data"].get("file_id"),
                            segment["data"].get("file"),
                        )
                    ),
                    None,
                )
                if matched is None or not current():
                    return None
                if attachment.kind == "file":
                    method = "get_group_file_url" if key.kind == "group" else "get_private_file_url"
                    params = {
                        "group_id" if key.kind == "group" else "user_id": key.recipient_id,
                        "file_id": file_id,
                    }
                    if key.kind == "private" and (
                        file_hash := media_file_id(matched.get("file_hash"))
                    ):
                        params["file_hash"] = file_hash
                    matched = await self._transport.call(socket, method, params)
                url = platform_media_url(matched.get("url"))
                if url and current():
                    data = await self._transport.download_platform_media(socket, url)
                    if current() and (mime := attachment_mime(data, attachment.name)):
                        return data, mime
        except (
            OSError,
            TimeoutError,
            WebSocketException,
            OneBotError,
            DesktopError,
            SQLiteError,
        ) as error:
            logger.debug("QQ 附件暂不可用（%s）", type(error).__name__)
        return None

    def _save_inbox(
        self,
        perception: Perception,
        route: MessageRoute,
        payload: dict[str, Any],
        *,
        archive: str | None = None,
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
        sender_name = (card or nickname) if group else nickname
        conversation_name = (name or self._group_names.get(group_id, "")) if group else name
        try:
            if archive is not None:
                self._inbox_store.archive(
                    perception,
                    sender_name=sender_name,
                    conversation_name=conversation_name,
                    notice=archive == "notice",
                )
                return True
            message = self._inbox_store.add(
                perception,
                sender_name=sender_name,
                conversation_name=conversation_name,
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
        if self._closing:
            raise OneBotError("QQ 正在关闭，没有开始新的发送")
        if route is None:
            if self._test_group is not None or self._config.allowed_group_id is not None:
                raise OneBotError("QQ 范围内的消息需要明确目的地址")
            return
        if not self._allows_route(route):
            raise OneBotError("QQ 目的地超出允许范围")
        if (
            message.channel != QQ_CHANNEL_NAME
            or route.kind not in {"group", "private"}
            or account_id(route.recipient_id) is None
        ):
            raise OneBotError("QQ 回址无效")
        if message.qq_action is not None and (
            not self._config.chat_actions_enabled
            or route.temporary_group_id is not None
            or message.emoji_digest is not None
        ):
            raise OneBotError("此会话未获准发送 QQ 动作")
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
            self._in_flight.discard(finished)
            if self._last_send.get(conversation) is finished:
                del self._last_send[conversation]
            if finished.cancelled() and message.report_delivery_failure is not None:
                message.report_delivery_failure("QQ 队列中的发送已取消，未投递这段回复。")

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

    def set_asset_reader(
        self,
        read: Callable[[ConversationKey, str], tuple[str, bytes] | None],
        allows: Callable[[ConversationKey, str], bool],
    ) -> None:
        """仅接收按完整目的会话复核的素材库。"""
        self._read_asset, self._allows_asset = read, allows

    async def list_groups(self, perception: Perception) -> list[tuple[str, str]]:
        """只有仍绑定的主人、本账号普通私聊可读取机器人实际所在群。"""
        socket = self._transport.socket

        def allowed() -> bool:
            route = perception.route
            return bool(
                socket is not None
                and self._transport.socket is socket
                and route is not None
                and route.kind == "private"
                and route.temporary_group_id is None
                and route.recipient_id == perception.sender_id
                and perception.trust is TrustLevel.VERIFIED_USER
                and self._is_owner(perception.sender_id)
                and self.allows_inbox_perception(perception)
            )

        if socket is None or not allowed():
            raise OneBotError("群名单不可用")
        groups = await self._transport.group_list(socket)
        if not allowed():
            raise OneBotError("群名单不可用")
        return [
            (identifier, name)
            for item in groups
            if isinstance(item, dict)
            and (identifier := account_id(item.get("group_id")))
            and (name := short_text(item.get("group_name"), 80))
        ]

    async def _send(
        self,
        socket: ClientConnection,
        message: OutgoingMessage,
        route: MessageRoute,
        previous: asyncio.Task[None] | None,
    ) -> None:
        identifier = None
        digest = message.emoji_digest
        action = message.qq_action
        key = ConversationKey(
            message.channel,
            route.account_id,
            route.kind,
            route.recipient_id,
            route.temporary_group_id,
        )
        store = self._inbox_store

        def authorize() -> None:
            if (
                self._closing
                or self._transport.socket is not socket
                or not self.allows_inbox_conversation(key)
                or (message.can_deliver is not None and not message.can_deliver())
                or (
                    (action is not None or digest is not None)
                    and self._allows_emoji_delivery is not None
                    and not self._allows_emoji_delivery()
                )
                or (
                    action is not None
                    and (
                        not self._config.chat_actions_enabled or key.temporary_group_id is not None
                    )
                )
            ):
                raise OneBotError("连接、原件或权限已失效")
            if action is not None and action.kind == "send_asset":
                if original(route.message_id).perception.has_shared_content:
                    raise OneBotError("转发或卡片里的引文不是当前发件人的素材请求")
                if self._allows_asset is None or not self._allows_asset(key, action.target_id):
                    raise OneBotError("当前会话已无该素材授权")
            if (
                digest is not None
                and self._allows_emoji_asset is not None
                and not self._allows_emoji_asset(digest)
            ):
                raise OneBotError("此表情已失去登记或外发资格")

        def authorize_send() -> None:
            authorize()
            task = asyncio.current_task()
            assert task is not None
            self._in_flight.add(task)

        def original(identifier: str) -> InboxMessage:
            row = store.find_platform_message(key, identifier) if store else None
            if row is None or row.perception.visibility != message.visibility:
                raise OneBotError("目标原件已不可用")
            return row

        try:
            if previous is not None:
                await asyncio.shield(previous)
            authorize()
            image = None
            if digest is not None:
                if self._read_emoji is None or not self._allows_emoji(message, route):
                    if message.report_delivery_failure is not None:
                        message.report_delivery_failure("表情不满足发送条件，未投递。")
                    return
                image = await asyncio.to_thread(self._read_emoji, digest)
                if (
                    image is None
                    or self._transport.socket is not socket
                    or not self._allows_emoji(message, route)
                ):
                    if message.report_delivery_failure is not None:
                        message.report_delivery_failure("表情或 QQ 连接不可用，未投递。")
                    return
            method = "send_msg"
            params: dict[str, Any] = {}
            if action is not None:
                dedup = (key, f"{route.message_id}:{action}")
                if dedup in self._recent_actions:
                    raise OneBotError("同一原件的相同动作已处理")
                self._recent_actions[dedup] = None
                if len(self._recent_actions) > _RECENT_MESSAGE_LIMIT:
                    self._recent_actions.popitem(last=False)
                if action.kind in {"at", "poke"}:
                    notice = self._recent_notices.get((key, route.message_id))
                    received_poke = bool(
                        notice
                        and notice.components[0].kind == "poke"
                        and notice.components[0].target_account_id == key.account_id
                        and notice.sender_id == action.target_id
                    )
                    if received_poke:
                        if not self._config.poke_response_enabled:
                            raise OneBotError("被戳回应已关闭")
                    elif (
                        original(action.source_message_id or route.message_id).perception.sender_id
                        != action.target_id
                    ):
                        raise OneBotError("动作对象不是消息来源")
                    if key.kind == "group":
                        await self.desktop.verify_member(socket, key, action.target_id)
                    elif (
                        action.kind != "poke"
                        or not received_poke
                        or action.target_id != key.recipient_id
                    ):
                        raise OneBotError("私聊只能回戳当前对方")
                    if not received_poke:
                        original(action.source_message_id or route.message_id)
                    if action.kind == "poke":
                        if not self._cooldown(key, action.target_id, "send"):
                            raise OneBotError("戳一戳仍在冷却")
                        method = "send_poke"
                        params = {"user_id": action.target_id}
                        if key.kind == "group":
                            params["group_id"] = key.recipient_id
                elif action.kind in {"react", "recall", "resend_image"}:
                    row = original(action.target_id)
                    if action.kind == "recall" and (
                        row.direction != "outgoing" or row.perception.sender_id != key.account_id
                    ):
                        raise OneBotError("只能撤回自己的已送达消息")
                    if action.kind == "react" and (
                        key.kind != "group" or emoji_id(action.emoji_id) is None
                    ):
                        raise OneBotError("贴表情的会话或表情无效")
                    verified = await self.desktop.verified_message_data(socket, key, row)
                    if action.kind == "recall":
                        method, params = "delete_msg", {"message_id": action.target_id}
                    elif action.kind == "react":
                        method, params = (
                            "set_msg_emoji_like",
                            {
                                "message_id": action.target_id,
                                "emoji_id": action.emoji_id,
                                "set": True,
                            },
                        )
                    else:
                        attachments = row.perception.attachments
                        index = action.attachment_index
                        if (
                            not (0 <= index < len(attachments))
                            or attachments[index].kind != "image"
                        ):
                            raise OneBotError("指定位置没有原图")
                        attachment = attachments[index]
                        actual = read_message_segments(verified.get("message"), key.account_id)[3]
                        if index >= len(actual) or actual[index] != attachment:
                            raise OneBotError("QQ 未确认指定位置的原图")
                        media = await self.read_inbox_attachment(row.perception, attachment)
                        if media is None:
                            raise OneBotError("指定原图已不可用")
                        image = media[0]
                        if attachment.is_emoji:
                            digest = sha256(image).hexdigest()
                            if (
                                self._read_emoji is None
                                or self._allows_emoji_asset is None
                                or not self._allows_emoji(message, route)
                            ):
                                raise OneBotError("表情原图未获准外发")
                            image = await asyncio.to_thread(self._read_emoji, digest)
                            if image is None:
                                raise OneBotError("表情原图没有已获准的素材原件")
                            message = replace(message, emoji_digest=digest)
                    original(action.target_id)
                elif action.kind == "send_asset":
                    if self._read_asset is None:
                        raise OneBotError("没有已授权素材库")
                    asset = await asyncio.to_thread(self._read_asset, key, action.target_id)
                    if asset is None or asset[0] != action.target_id:
                        raise OneBotError("当前会话素材未获准")
                    name, data = asset
                    if _image_mime(data):
                        image = data
                    else:
                        await self.desktop.upload_file(key, name, data, authorize_send)
                        method = "uploaded"
            if method == "send_msg":
                params = encode_message(message, route, image=image)
            if method != "uploaded":
                authorize_send()
                receipt = await self._transport.call(socket, method, params)
                if method == "send_msg":
                    identifier = message_id(receipt.get("message_id"))
                    if identifier is None:
                        raise OneBotError("QQ 未返回有效消息编号")
        except TimeoutError:
            logger.warning("等待 QQ 投递确认超时，无法确认是否送达", exc_info=True)
            if message.report_delivery_failure is not None:
                message.report_delivery_failure("等待 QQ 投递确认超时，无法确认这段回复是否送达。")
            return
        except (
            OSError,
            WebSocketException,
            OneBotError,
            DesktopError,
            ValueError,
            SQLiteError,
        ) as error:
            logger.warning("QQ 发送失败（%s）", type(error).__name__)
            if message.report_delivery_failure is not None:
                message.report_delivery_failure("QQ 未确认这段回复投递成功。")
            return
        if identifier is not None and message.confirm_platform_delivery is not None:
            message.confirm_platform_delivery(identifier)
        elif message.confirm_delivery is not None:
            message.confirm_delivery()
        if identifier is not None:
            await self._save_delivered(message, route, identifier, params["message"])
        elif method == "delete_msg" and store is not None and action is not None:
            await asyncio.to_thread(store.mark_recalled, key, action.target_id)
        try:
            if digest is not None and self._record_emoji_use is not None:
                await asyncio.to_thread(self._record_emoji_use, digest)
        except SQLiteError:
            logger.exception("QQ 消息已发送，但外发附属记录写入失败")

    async def _save_delivered(
        self, message: OutgoingMessage, route: MessageRoute, receipt_id: object, segments: object
    ) -> None:
        if self._inbox_store is None:
            return
        identifier = message_id(receipt_id)
        if identifier is None:
            logger.warning("QQ 发送回执缺少有效消息键，无法归档外发消息")
            return
        text, _, quoted, _, components = read_message_segments(segments, route.account_id)
        perception = Perception(
            message.channel,
            route.account_id,
            "[表情]" if message.emoji_digest is not None else text or message.content or "[图片]",
            message.visibility,
            TrustLevel.VERIFIED_CONTACT,
            route=replace(route, message_id=identifier),
            quoted_message_id=quoted,
            source_message_id=message_id(route.message_id)
            if message.timeline_id is not None
            else None,
            components=components,
        )
        await asyncio.to_thread(
            self._inbox_store.add_delivered, perception, sender_name=message.sender_name
        )
