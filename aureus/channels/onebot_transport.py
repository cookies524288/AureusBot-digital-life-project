"""通道 channels：OneBot 正向 WebSocket 的连接、动作和 echo。

协议参考 snowluma-adapter 1.1.8（GPL-v3.0-or-later）；对应路径见 THIRD_PARTY.md。
"""

import asyncio
import json
import logging
from base64 import b64decode
from collections.abc import Callable
from dataclasses import dataclass, field
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
_IMAGE_DOWNLOAD_ACTION = "download_file_image_stream"
_IMAGE_LIMIT = 8 * 1024 * 1024
_IMAGE_CHUNK_SIZE = 64 * 1024


class OneBotError(RuntimeError):
    """OneBot 动作失败或连接不可用"""


def decode_media_base64(value: object, limit: int) -> bytes | None:
    """在解码前后检查附件长度，拒绝畸形数据。"""
    if not isinstance(value, str) or len(value) > (limit + 2) // 3 * 4:
        return None
    try:
        result = b64decode(value, validate=True)
    except ValueError:
        return None
    return result if 0 < len(result) <= limit else None


@dataclass
class _ImageStream:
    """组合同一 echo 的文件头、连续数据块和结束帧。"""

    result: asyncio.Future[bytes | None]
    data: bytearray = field(default_factory=bytearray)
    declared_size: int | None = None
    chunks: int = 0

    def receive(self, response: dict[str, Any]) -> None:
        packet = response.get("data")
        if (
            response.get("status") != "ok"
            or response.get("retcode") != 0
            or response.get("stream") != "stream-action"
            or not isinstance(packet, dict)
        ):
            self.result.set_result(None)
            return
        match packet.get("type"), packet.get("data_type"):
            case "stream", "file_info" if self.declared_size is None:
                size = packet.get("file_size")
                if type(size) is int and 0 <= size <= _IMAGE_LIMIT:
                    self.declared_size = size
                    return
            case "stream", "file_chunk" if self.declared_size is not None:
                if self._append(packet):
                    return
            case "response", "file_complete" if self.declared_size is not None:
                if (
                    self.data
                    and packet.get("total_bytes") == len(self.data)
                    and packet.get("total_chunks") == self.chunks
                ):
                    self.result.set_result(bytes(self.data))
                    return
        self.result.set_result(None)

    def _append(self, packet: dict[str, Any]) -> bool:
        if packet.get("index") != self.chunks:
            return False
        chunk = decode_media_base64(packet.get("data"), _IMAGE_LIMIT - len(self.data))
        if chunk is None or packet.get("size") != len(chunk):
            return False
        self.data.extend(chunk)
        self.chunks += 1
        return True


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
        self._image_streams: dict[str, _ImageStream] = {}

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
                    if not isinstance(echo, str):
                        continue
                    stream = self._image_streams.get(echo)
                    if stream is not None and not stream.result.done():
                        stream.receive(payload)
                        continue
                    future = self._pending.get(echo)
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
            for stream in self._image_streams.values():
                if not stream.result.done():
                    stream.result.set_result(None)

    async def download_image(self, socket: ClientConnection, file_id: str) -> bytes:
        """从已核对的连接取 SnowLuma 缓存中的图片，限制等待时间与字节数。"""
        echo = uuid4().hex
        result: asyncio.Future[bytes | None] = asyncio.get_running_loop().create_future()
        self._image_streams[echo] = _ImageStream(result)
        try:
            async with asyncio.timeout(self._config.action_timeout_seconds):
                await socket.send(
                    json.dumps(
                        {
                            "action": _IMAGE_DOWNLOAD_ACTION,
                            "params": {"file_id": file_id, "chunk_size": _IMAGE_CHUNK_SIZE},
                            "echo": echo,
                        }
                    )
                )
                media = await result
        finally:
            self._image_streams.pop(echo, None)
        if media is None:
            raise OneBotError("OneBot 图片下载失败")
        return media

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
