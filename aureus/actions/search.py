"""搜索接口；真实服务留待用户选定，测试注入假搜索（D79）。"""

from dataclasses import dataclass
from typing import Protocol


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
