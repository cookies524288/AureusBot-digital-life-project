"""限时限量、逐跳检查公网地址的只读网页工具（D79；接受设计中的 DNS 重绑定风险）。"""

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from aureus import __version__
from aureus.common import ExplorationConfig

logger = logging.getLogger(__name__)
_MAX_REDIRECTS = 3
_CHUNK_BYTES = 8192
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_IGNORED_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "section", "article", "tr"}


class FetchFailure(StrEnum):
    """网页读取失败的固定原因。"""

    BAD_URL = "bad_url"
    BLOCKED_ADDRESS = "blocked_address"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    TIMEOUT = "timeout"
    NOT_TEXT = "not_text"
    HTTP_ERROR = "http_error"
    NETWORK = "network"


@dataclass(frozen=True)
class WebPage:
    """最终网址、标题及截断后的正文。"""

    url: str
    title: str
    text: str
    truncated: bool


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self.ignored = 0
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _IGNORED_TAGS:
            self.ignored += 1
        if tag == "title":
            self.in_title = True
        if tag in _BLOCK_TAGS and not self.ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _IGNORED_TAGS:
            self.ignored = max(0, self.ignored - 1)
        if tag == "title":
            self.in_title = False
        if tag in _BLOCK_TAGS and not self.ignored:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            (self.title if self.in_title else self.parts).append(data)


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class WebReader:
    """不持有凭据、不写时间线；任何读取错误都返回失败枚举。"""

    def __init__(
        self,
        config: ExplorationConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.config, self._transport, self._resolve = config, transport, resolve or _resolve

    async def _check_url(self, url: str) -> FetchFailure | None:
        try:
            parts = urlsplit(url)
            if (
                parts.scheme not in ("http", "https")
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.port not in (None, 80 if parts.scheme == "http" else 443)
            ):
                return FetchFailure.BAD_URL
            try:
                addresses = [str(ipaddress.ip_address(parts.hostname))]
            except ValueError:
                addresses = await self._resolve(parts.hostname)
            if not addresses:
                return FetchFailure.BLOCKED_ADDRESS
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                    ip = ip.ipv4_mapped
                if not ip.is_global or ip.is_multicast or ip.is_reserved:
                    return FetchFailure.BLOCKED_ADDRESS
        except ValueError:
            return FetchFailure.BAD_URL
        return None

    async def read(self, url: str) -> WebPage | FetchFailure:
        """整次读取共用超时；跳转逐跳检查，错误不抛给心跳。"""
        try:
            async with asyncio.timeout(self.config.fetch_timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    cookies=None,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    client.headers.clear()
                    client.headers.update(
                        {
                            "User-Agent": f"AureusBot/{__version__} (+read-only)",
                            "Accept": "text/html, text/plain",
                        }
                    )
                    return await self._read(client, url)
        except (TimeoutError, httpx.TimeoutException):
            return FetchFailure.TIMEOUT
        except httpx.InvalidURL:
            return FetchFailure.BAD_URL
        except Exception:
            logger.exception("读取网页失败")
            return FetchFailure.NETWORK

    async def _read(self, client: httpx.AsyncClient, url: str) -> WebPage | FetchFailure:
        for hop in range(_MAX_REDIRECTS + 1):
            failure = await self._check_url(url)
            if failure is not None:
                return failure
            # 同一个读取里的响应也可能设 cookie；每跳都清空，不带去下一跳（D79）。
            client.cookies.clear()
            async with client.stream("GET", url) as response:
                if response.status_code in _REDIRECT_CODES:
                    if hop == _MAX_REDIRECTS:
                        return FetchFailure.TOO_MANY_REDIRECTS
                    location = response.headers.get("location")
                    if not location:
                        return FetchFailure.BAD_URL
                    url = urljoin(url, location)
                    continue
                if not response.is_success:
                    return FetchFailure.HTTP_ERROR
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in ("text/html", "text/plain"):
                    return FetchFailure.NOT_TEXT
                body = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes(chunk_size=_CHUNK_BYTES):
                    remaining = self.config.max_page_bytes - len(body)
                    body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        break
                text = body.decode(response.encoding or "utf-8", errors="replace")
                title = ""
                if kind == "text/html":
                    parser = _TextExtractor()
                    parser.feed(text)
                    title = " ".join("".join(parser.title).split())
                    text = "\n".join(
                        " ".join(line.split())
                        for line in "".join(parser.parts).splitlines()
                        if line.strip()
                    )
                return WebPage(
                    str(response.url),
                    title,
                    text[: self.config.max_page_chars],
                    truncated or len(text) > self.config.max_page_chars,
                )
        return FetchFailure.TOO_MANY_REDIRECTS
