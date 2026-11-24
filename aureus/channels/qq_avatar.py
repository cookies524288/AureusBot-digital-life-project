"""读取 SnowLuma 明确返回的 QQ 头像；浏览器不接触上游 URL。"""

import asyncio
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from websockets.asyncio.client import ClientConnection

from aureus.common import Clock
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage

from .onebot import account_id
from .qq_desktop import DesktopError, QQDesktop

Avatar = tuple[bytes, str]
_MAX_BYTES = 128 * 1024
_CACHE_SIZE = 64
_CACHE_AGE = timedelta(minutes=5)
_FETCH_TIMEOUT = 5
_SOURCE = re.compile(r"https://q1?\.qlogo\.cn/g\?b=qq&nk=([1-9][0-9]{0,19})&s=100")


@dataclass(frozen=True)
class _CachedAvatar:
    expires: datetime
    media: Avatar | None


class QQAvatars:
    """只查询当前可见原件上的发件人，使用会话级、有界、短期内存缓存。"""

    def __init__(
        self,
        desktop: QQDesktop,
        clock: Clock,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._desktop = desktop
        self._clock = clock
        self._transport = transport
        self._cache: OrderedDict[tuple[ConversationKey, str], _CachedAvatar] = OrderedDict()
        self._lookup = asyncio.Lock()

    async def read(self, row: InboxMessage, authorized: Callable[[], bool]) -> Avatar | None:
        """读取绑定到当前账号和会话中可证实的发件人头像。"""
        key = row.conversation
        sender = account_id(row.perception.sender_id)
        if not authorized() or sender is None or row.direction == "notice":
            return None
        if key.kind == "private" and sender not in {key.account_id, key.recipient_id}:
            return None
        try:
            socket = self._desktop.ready(key)
            async with self._lookup:
                if not self._current(key, socket, authorized):
                    return None
                cache_key = key, sender
                cached = self._cache.get(cache_key)
                if cached is not None and cached.expires > self._clock.now():
                    self._cache.move_to_end(cache_key)
                    return cached.media
                source = await self._desktop.avatar_source(socket, row, sender, authorized)
                if not self._current(key, socket, authorized):
                    return None
                media = await self._fetch(source, sender) if source else None
                if not self._current(key, socket, authorized):
                    return None
                self._cache[cache_key] = _CachedAvatar(self._clock.now() + _CACHE_AGE, media)
                self._cache.move_to_end(cache_key)
                if len(self._cache) > _CACHE_SIZE:
                    self._cache.popitem(last=False)
                return media
        except DesktopError:
            return None

    def _current(
        self,
        key: ConversationKey,
        socket: ClientConnection,
        authorized: Callable[[], bool],
    ) -> bool:
        return authorized() and self._desktop.ready(key) is socket

    async def _fetch(self, source: str, sender: str) -> Avatar | None:
        match = _SOURCE.fullmatch(source)
        if match is None or match[1] != sender:
            return None
        try:
            async with (
                asyncio.timeout(_FETCH_TIMEOUT),
                httpx.AsyncClient(
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                    headers={
                        "Accept": "image/png,image/jpeg,image/gif,image/webp",
                        "Accept-Encoding": "identity",
                    },
                ) as client,
                client.stream("GET", source) as response,
            ):
                if response.status_code != 200 or response.headers.get("Content-Encoding"):
                    return None
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(chunk) > _MAX_BYTES - len(data):
                        return None
                    data.extend(chunk)
                mime = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
                if _has_signature(data, mime):
                    return bytes(data), mime
        except (httpx.HTTPError, TimeoutError):
            return None
        return None


def _has_signature(data: bytearray, mime: str) -> bool:
    match mime:
        case "image/png":
            return data.startswith(b"\x89PNG\r\n\x1a\n")
        case "image/jpeg":
            return data.startswith(b"\xff\xd8\xff")
        case "image/gif":
            return data.startswith((b"GIF87a", b"GIF89a"))
        case "image/webp":
            return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        case _:
            return False
