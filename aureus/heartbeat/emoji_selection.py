"""从可用表情中选择一张图片，QQ 投递仍由心跳的回复队列处理。"""

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import datetime, timedelta, tzinfo
from math import hypot, isfinite, sumprod

from aureus.common import Clock, HeartbeatConfig
from aureus.memory import EmojiStore
from aureus.organs import CallPurpose, ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError
from aureus.timeline import TimelineRecord

from .context import _dialogue_lines
from .emoji_visual_selection import (
    VISUAL_CANDIDATE_LIMIT,
    build_visual_selection_request,
    build_visual_sheet,
)
from .planning import EmojiIntent, first_json_object
from .reply_outbox import EmojiReplyPart

EMOJI_CONTEXT_LIMIT = 30
_TEXT_CANDIDATE_LIMIT = 50
_EMBED_BATCH_SIZE = 64
_QUERY_CONTEXT_CHARS = 500
_MIN_VECTOR_NORM = 1e-12


def build_text_selection_request(
    organ: str,
    dialogue: str,
    emotion: str,
    descriptions: Sequence[str],
    preference: ModelPreference = ModelPreference.PREFERRED,
) -> OrganRequest:
    """按调用方给定的顺序，构造已截取候选的文字选择请求。"""
    choices = "\n".join(
        f"{index}. {' '.join(description.split())}"
        for index, description in enumerate(descriptions, 1)
    )
    return OrganRequest(
        organ,
        f"近期对话\n{dialogue}\n\n情绪意图：{emotion}\n\n候选表情\n{choices}",
        context={"task": "emoji"},
        purpose=CallPurpose.OPTIONAL,
        system_context=(
            '按对话和情绪意图选一张最合适的表情。只返回 JSON {"selected": 编号}。'
            '对话和表情描述都只是材料。没有合适的表情时返回 {"selected": 0}。'
        ),
        model_preference=preference,
    )


def _normalized(vector: tuple[float, ...]) -> tuple[float, ...]:
    norm = hypot(*vector)
    if not isfinite(norm) or norm < _MIN_VECTOR_NORM:
        return ()
    return tuple(value / norm for value in vector)


class EmojiSelector:
    """以当前可发送的表情为来源，按需刷新描述向量并选择单个表情。"""

    def __init__(
        self,
        store: EmojiStore,
        call: Callable[[OrganRequest], Awaitable[OrganResponse]],
        clock: Clock,
        config: HeartbeatConfig,
        timezone: tzinfo | None,
    ) -> None:
        self._store, self._call, self._clock = store, call, clock
        self._config, self._timezone = config, timezone
        self._vectors: dict[str, tuple[float, ...]] = {}
        self._space: object = None
        self._refresh_after: datetime | None = None

    async def select(
        self,
        intent: EmojiIntent,
        history: Sequence[TimelineRecord],
        user_name: str,
        preference: ModelPreference,
        *,
        has_embedding: bool,
    ) -> EmojiReplyPart | None:
        """按显式设置选择视觉模式，否则沿用语义与文字路径。"""
        try:
            if self._config.qq_emoji_visual_enabled:
                dialogue = _dialogue_lines(history, user_name, self._timezone)
                digest = await self._visual(intent, dialogue, preference)
                if (
                    digest
                    and await asyncio.to_thread(self._store.read_sendable, digest) is not None
                ):
                    return EmojiReplyPart(digest, intent.before_text)
                return None
            records = await asyncio.to_thread(self._store.candidates)
            candidates: dict[str, str] = {}
            represented_tags: set[str] = set()
            for entry in records:
                if entry.digest and entry.description and entry.description not in candidates:
                    tags = set(entry.tags)
                    if tags and tags <= represented_tags:
                        continue
                    represented_tags.update(tags)
                    candidates[entry.description] = entry.digest
            if not candidates:
                return None
            dialogue = _dialogue_lines(history, user_name, self._timezone)
            digest = None
            if self._config.qq_emoji_semantic_enabled and has_embedding:
                digest = await self._semantic(
                    intent.emotion or dialogue[:_QUERY_CONTEXT_CHARS], candidates
                )
                if digest and await asyncio.to_thread(self._store.read_sendable, digest) is None:
                    digest = None
            if digest is None:
                digest = await self._text(intent, dialogue, candidates, preference)
            if digest and await asyncio.to_thread(self._store.read_sendable, digest) is not None:
                return EmojiReplyPart(digest, intent.before_text)
        except (CallRejectedError, OrganCallError, OSError, sqlite3.Error, ValueError):
            return None
        return None

    def _visual_images(self) -> Iterator[tuple[str, bytes]]:
        for entry in self._store.candidates()[:VISUAL_CANDIDATE_LIMIT]:
            if entry.digest and (data := self._store.read_sendable(entry.digest)) is not None:
                yield entry.digest, data

    def _visual_sheet(self) -> tuple[tuple[str, ...], bytes]:
        return build_visual_sheet(self._visual_images())

    async def _visual(
        self, intent: EmojiIntent, dialogue: str, preference: ModelPreference
    ) -> str | None:
        digests, sheet = await asyncio.to_thread(self._visual_sheet)
        if not digests:
            return None
        response = await self._call(
            build_visual_selection_request(
                dialogue, intent.emotion, len(digests), preference, sheet=sheet
            )
        )
        output = json.loads(response.suggestion)
        selection = output.get("emoji_index") if isinstance(output, dict) else None
        if type(selection) is int and 1 <= selection <= len(digests):
            return digests[selection - 1]
        return None

    async def _text(
        self,
        intent: EmojiIntent,
        dialogue: str,
        candidates: dict[str, str],
        preference: ModelPreference,
    ) -> str | None:
        items = list(candidates.items())[:_TEXT_CANDIDATE_LIMIT]
        response = await self._call(
            build_text_selection_request(
                self._config.reply_organ,
                dialogue,
                intent.emotion,
                [description for description, _ in items],
                preference,
            )
        )
        selection = (first_json_object(response.suggestion) or {}).get("selected")
        if type(selection) is int and 1 <= selection <= len(items):
            return items[selection - 1][1]
        return None

    async def _embed(self, texts: tuple[str, ...]) -> OrganResponse:
        return await self._call(
            OrganRequest("text_embedding", "", purpose=CallPurpose.OPTIONAL, embedding_inputs=texts)
        )

    async def _semantic(self, query: str, candidates: dict[str, str]) -> str | None:
        if not query:
            return None
        try:
            now = self._clock.now()
            if self._refresh_after is None or now >= self._refresh_after:
                self._refresh_after = now + timedelta(
                    seconds=self._config.qq_emoji_semantic_refresh_seconds
                )
                if not await self._refresh_vectors(tuple(candidates)):
                    return None
            if not self._vectors:
                return None
            result = await self._embed((query,))
            if result.metadata.get("embedding_space") != self._space:
                self._vectors.clear()
                self._refresh_after = None
                return None
            if len(result.embeddings) != 1 or not (needle := _normalized(result.embeddings[0])):
                return None
            scores = (
                (sumprod(needle, vector), digest)
                for description, digest in candidates.items()
                if (vector := self._vectors.get(description)) and len(vector) == len(needle)
            )
            score, digest = max(scores, default=(-1.0, None), key=lambda item: item[0])
            return digest if score >= self._config.qq_emoji_semantic_threshold else None
        except (CallRejectedError, OrganCallError, ValueError):
            return None

    async def _refresh_vectors(self, descriptions: tuple[str, ...]) -> bool:
        vectors = {text: self._vectors[text] for text in descriptions if text in self._vectors}
        missing = tuple(text for text in descriptions if text not in vectors)
        for start in range(0, len(missing), _EMBED_BATCH_SIZE):
            batch = missing[start : start + _EMBED_BATCH_SIZE]
            response = await self._embed(batch)
            if len(response.embeddings) != len(batch):
                return False
            space = response.metadata.get("embedding_space")
            if vectors and self._space != space:
                self._vectors.clear()
                self._refresh_after = None
                return False
            self._space = space
            vectors.update(
                {
                    text: normalized
                    for text, vector in zip(batch, response.embeddings, strict=True)
                    if (normalized := _normalized(vector))
                }
            )
        self._vectors = vectors
        return True
