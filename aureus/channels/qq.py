"""通道 channels：把通过账号核对的 QQ 消息送入心跳"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import Any, Self

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import WebSocketException

from aureus.common import QQConfig
from aureus.heartbeat import MessageKind, OutgoingMessage, Perception, TrustLevel, VisibilityScope

from .onebot import QQ_CHANNEL_NAME, decode_message, encode_message
from .onebot_transport import OneBotError, OneBotTransport
from .web import HeartbeatHandle

logger = logging.getLogger(__name__)
# 跨连接保留最近收取的消息，限制进程长期运行的占用。
_RECENT_MESSAGE_LIMIT = 16_384
_OUTGOING_LIMIT = 64


class QQChannel:
    """把 SnowLuma 事件翻译成感知，把心跳的回复送回原会话"""

    def __init__(
        self,
        config: QQConfig,
        token: SecretStr | None,
        heartbeat: HeartbeatHandle,
        is_owner: Callable[[str], bool],
        consume_command: Callable[[Perception], bool] | None = None,
    ) -> None:
        self._config = config
        self._heartbeat = heartbeat
        self._is_owner = is_owner
        self._consume_command = consume_command
        self._transport = OneBotTransport(
            config, token.get_secret_value() if token else None, self._receive
        )
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._sending: set[asyncio.Task[None]] = set()
        self._connection: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._connection = asyncio.create_task(self._transport.run(), name="aureus-onebot")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        tasks = list(self._sending)
        if self._connection is not None:
            tasks.append(self._connection)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _receive(self, payload: dict[str, Any]) -> None:
        perception = decode_message(payload, self._config.account_id)
        if perception is None or perception.route is None:
            return
        route = perception.route
        key = QQ_CHANNEL_NAME, route.account_id, route.message_id
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
            if not self._heartbeat.submit(perception):
                return
        self._seen[key] = None
        if len(self._seen) > _RECENT_MESSAGE_LIMIT:
            self._seen.popitem(last=False)

    def show(self, message: OutgoingMessage) -> None:
        """把已通过心跳检查的回复交给后台发送，避免网络阻塞心跳"""
        route = message.route
        if message.kind is not MessageKind.REPLY or route is None:
            return
        socket = self._transport.socket
        if socket is None or len(self._sending) >= _OUTGOING_LIMIT:
            raise OneBotError("QQ 连接未就绪或发送队列已满")
        task = asyncio.create_task(
            self._send(socket, encode_message(message, route)), name="aureus-onebot-send"
        )
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _send(self, socket: ClientConnection, params: dict[str, Any]) -> None:
        try:
            await self._transport.call(socket, "send_msg", params)
        except (OSError, TimeoutError, WebSocketException, OneBotError) as error:
            logger.warning("QQ 发送失败（%s）", type(error).__name__)
