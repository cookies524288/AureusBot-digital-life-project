"""探索使用的只读搜索接口和 SearXNG JSON 客户端。"""

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class SearchResult:
    """搜索返回的标题、网址和摘要。"""

    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    """只读搜索接口。"""

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        """返回按服务原顺序排列的结果。"""
        ...


class NoSearch:
    """未配置搜索；运行时传 None，保留这个空实现供接口使用。"""

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        """没有搜索服务时返回空列表。"""
        return []


class SearxngSearch:
    """从维护者配置的 SearXNG 搜索端点取得结果。"""

    def __init__(
        self, endpoint: str, timeout: float, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._transport = transport

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        """按原排序返回 JSON 结果，失败交由探索流程记账。"""
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.get(self._endpoint, params={"q": query, "format": "json"})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("SearXNG 搜索响应缺少结果列表")
        matches: list[SearchResult] = []
        for item in payload["results"]:
            if len(matches) >= limit:
                break
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = item.get("title")
            snippet = item.get("content")
            matches.append(
                SearchResult(
                    title if isinstance(title, str) else "",
                    url,
                    snippet if isinstance(snippet, str) else "",
                )
            )
        return matches
