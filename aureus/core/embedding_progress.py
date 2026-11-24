"""组装运行中的嵌入覆盖统计；只读，不启动向量生成。"""

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from aureus.channels import CHANNEL_NAME, TERMINAL_USER_ID
from aureus.common.web_identity import WEB_CHANNEL_NAME, WEB_USER_ID
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat.emoji_selection import EmojiSelector
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.memory import ExpressionVectorIndex, LanguageStore
from aureus.memory.expression_index import ExpressionSpaceCoverage

_LOCAL_EXPRESSION_SCOPES = frozenset(
    json.dumps(source, separators=(",", ":"))
    for source in ((WEB_CHANNEL_NAME, WEB_USER_ID), (CHANNEL_NAME, TERMINAL_USER_ID))
)

type ProgressKind = Literal["expression", "images", "emoji_cache", "memory_rerank"]
type ProgressState = Literal[
    "disabled", "no_model", "unavailable", "on_demand", "empty", "missing", "partial", "complete"
]


@dataclass(frozen=True)
class IndexProgress:
    """可公开的统计，不含空间地址、模型名、资料或会话键。"""

    kind: ProgressKind
    state: ProgressState
    route: int | None = None
    established: int | None = None
    eligible: int | None = None


def _coverage(
    kind: ProgressKind, established: int, eligible: int, route: int | None = None
) -> IndexProgress:
    state: ProgressState
    if not eligible:
        state = "empty"
    elif not established:
        state = "missing"
    elif established < eligible:
        state = "partial"
    else:
        state = "complete"
    return IndexProgress(kind, state, route, established, eligible)


@dataclass(frozen=True)
class EmbeddingProgress:
    """读取核心已装配的真实索引、选择器和已应用路线。"""

    models: ModelRuntime
    expression_enabled: bool = False
    language: LanguageStore | None = None
    expression_index: ExpressionVectorIndex | None = None
    emoji_selector: EmojiSelector | None = None
    memory_rerank_enabled: bool = False
    inbox: InboxStore | None = None
    images: InboxImageIndex | None = None
    allows_conversation: Callable[[ConversationKey], bool] | None = None

    async def read(self, organ: str) -> list[IndexProgress] | None:
        """返回可见计数；读取期间若配置切换，舍弃旧快照。"""
        spaces = self.models.embedding_spaces
        if organ == "image_embedding":
            result = await self._images(spaces.image)
        else:
            emoji = await self._emoji(spaces.text)
            rerank: ProgressState = "disabled"
            if self.memory_rerank_enabled:
                rerank = "on_demand" if spaces.text else "no_model"
            result = [
                await self._expression(spaces.expression),
                *emoji,
                IndexProgress("memory_rerank", rerank),
            ]
        return result if self.models.embedding_spaces is spaces else None

    async def _expression(self, space: str | None) -> IndexProgress:
        if not self.expression_enabled:
            return IndexProgress("expression", "disabled")
        if space is None:
            return IndexProgress("expression", "no_model")
        if self.expression_index is None or self.language is None:
            return IndexProgress("expression", "unavailable")
        scopes = await self.expression_scopes()
        established, eligible = await asyncio.to_thread(
            self.expression_index.coverage, self.language, space, scopes
        )
        if not scopes <= await self.expression_scopes():
            return IndexProgress("expression", "unavailable")
        return _coverage("expression", established, eligible)

    async def expression_scopes(self) -> frozenset[str]:
        """已登录本机来源及当前可查看、可使用的 QQ 会话范围。"""
        routes = (
            await asyncio.to_thread(self.inbox.visible_conversation_keys)
            if self.inbox is not None and self.allows_conversation is not None
            else []
        )
        allows = self.allows_conversation
        return _LOCAL_EXPRESSION_SCOPES.union(
            route.language_scope_key() for route in routes if allows is not None and allows(route)
        )

    async def expression_spaces(
        self, scopes: frozenset[str], available: str | None
    ) -> list[ExpressionSpaceCoverage]:
        """只读学习页的空间和覆盖，不触发计算。"""
        if self.language is None or self.expression_index is None:
            return []
        return await asyncio.to_thread(
            self.expression_index.spaces, self.language, scopes, available
        )

    async def _emoji(self, spaces: tuple[tuple[int, str], ...]) -> list[IndexProgress]:
        if self.emoji_selector is None:
            return [IndexProgress("emoji_cache", "disabled")]
        if not spaces:
            return [IndexProgress("emoji_cache", "no_model")]
        return [
            _coverage("emoji_cache", established, eligible, route)
            for route, established, eligible in await self.emoji_selector.cache_coverage(spaces)
        ]

    async def _images(self, spaces: tuple[tuple[int, str], ...]) -> list[IndexProgress]:
        if self.images is None or (allows := self.allows_conversation) is None:
            return [IndexProgress("images", "disabled")]
        if not spaces:
            return [IndexProgress("images", "no_model")]
        rows = await asyncio.to_thread(self.images.coverage_by_conversation)
        keys = {key for key, _, _ in rows}
        allowed = {key for key in keys if allows(key)}
        counts: Counter[str | None] = Counter()
        for key, space, count in rows:
            if key in allowed:
                counts[space] += count
        eligible = counts.total()
        return [_coverage("images", counts[space], eligible, number) for number, space in spaces]
