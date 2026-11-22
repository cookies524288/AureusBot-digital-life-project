"""通道 channels：OneBot 正向 WebSocket 的连接、动作和 echo。

协议参考 snowluma-adapter 1.1.8（GPL-v3.0-or-later）；对应路径见 THIRD_PARTY.md。
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from aureus.common import QQConfig

from .onebot import account_id

logger = logging.getLogger(__name__)
_wire_logger = logging.getLogger(f"{__name__}.wire")
# DEBUG 帧包含握手令牌、聊天正文及尚未消费的绑定验证码。
_wire_logger.setLevel(logging.INFO)


class OneBotError(RuntimeError):
    """OneBot 动作失败或连接不可用"""


class _Connect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # 握手携带密钥，只能访问部署者指定的端点。
        return exc


class OneBotTransport:
    """管理单个收发账号的 WebSocket 和在途动作"""

    def __init__(
        self, config: QQConfig, token: str | None, receive: Callable[[dict[str, Any]], None]
    ) -> None:
        self._config = config
        self._token = token
        self._receive = receive
        self.socket: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def run(self) -> None:
        """保持连接，断开后重新验证收发账号"""
        url = self._config.endpoint
        headers = None
        if self._token:
            url += "?" + urlencode({"access_token": self._token})
            headers = {"Authorization": f"Bearer {self._token}"}
        while True:
            try:
                async with _Connect(
                    url,
                    additional_headers=headers,
                    logger=_wire_logger,
                    proxy=None,
                    close_timeout=self._config.action_timeout_seconds,
                ) as socket:
                    await self._serve(socket)
            except (OSError, TimeoutError, WebSocketException, OneBotError) as error:
                logger.warning("QQ 连接结束（%s），等待重连", type(error).__name__)
            await asyncio.sleep(self._config.reconnect_seconds)

    async def _serve(self, socket: ClientConnection) -> None:
        ready = asyncio.Event()
        reader = asyncio.create_task(self._read(socket, ready), name="aureus-onebot-reader")
        try:
            login = await self.call(socket, "get_login_info", {})
            if account_id(login.get("user_id")) != self._config.account_id:
                raise OneBotError("OneBot 登录账号与部署配置不一致")
            self.socket = socket
            ready.set()
            await reader
        finally:
            self.socket = None
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def _read(self, socket: ClientConnection, ready: asyncio.Event) -> None:
        try:
            async for raw in socket:
                if not isinstance(raw, str):
                    continue
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if "echo" in payload:
                    echo = payload["echo"]
                    future = self._pending.get(echo) if isinstance(echo, str) else None
                    if future is not None and not future.done():
                        future.set_result(payload)
                        # 首个 echo 后可能紧接消息，等账号核对完再继续读。
                        await ready.wait()
                elif self.socket is socket:
                    self._receive(payload)
        finally:
            self.socket = None
            for future in self._pending.values():
                if not future.done():
                    # 用结果唤醒，避免发送本身失败时留下无人取回的 Future 异常。
                    future.set_result({})

    async def call(
        self, socket: ClientConnection, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """发送动作并等待同一连接上对应 echo，出错时结束等待"""
        echo = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            async with asyncio.timeout(self._config.action_timeout_seconds):
                await socket.send(json.dumps({"action": action, "params": params, "echo": echo}))
                response = await future
        finally:
            self._pending.pop(echo, None)
        if not response:
            raise OneBotError("OneBot 连接已断开")
        if response.get("status") != "ok" or response.get("retcode") != 0:
            raise OneBotError("OneBot 拒绝了动作")
        data = response.get("data")
        return data if isinstance(data, dict) else {}
