"""网页登录后的习得语言和长期记忆资源。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query

from aureus.memory import LanguageKind, LanguageStore, MemoryIndex

DEFAULT_SIZE = 30
MAX_SIZE = 100


def register_resource_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    language: LanguageStore | None,
    memory: MemoryIndex | None,
) -> None:
    """注册只读资源浏览，独立连接在工作线程中查询。"""

    @app.get("/api/resources/learning")
    async def browse_learning(
        _token: Annotated[str, Depends(require_ready)],
        kind: LanguageKind = LanguageKind.EXPRESSION,
        q: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        def query() -> list[dict[str, Any]]:
            if language is None:
                return []
            term = q.strip().casefold()
            matches = (
                entry
                for entry in language.entries(kind)
                if not term
                or any(
                    term in field.casefold() for field in (entry.text, entry.detail, entry.example)
                )
            )
            from itertools import islice

            return [asdict(entry) for entry in islice(matches, offset, offset + limit + 1)]

        rows = await asyncio.to_thread(query)
        return {
            "enabled": language is not None,
            "items": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @app.get("/api/resources/memory")
    async def browse_memory(
        _token: Annotated[str, Depends(require_ready)],
        kind: Literal["history", "live"] = "history",
        q: Annotated[str, Query(max_length=200)] = "",
        status: Literal[
            "", "active", "inactive", "conflicted", "superseded", "retracted", "deleted", "missing"
        ] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        def query() -> list[dict[str, Any]]:
            if memory is None:
                return []
            if kind == "live":
                return [asdict(row) for row in memory.browse_live(q.strip(), offset, limit + 1)]
            return [
                asdict(row) for row in memory.browse_history(q.strip(), status, offset, limit + 1)
            ]

        rows = await asyncio.to_thread(query)
        return {
            "enabled": memory is not None,
            "items": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
        }
