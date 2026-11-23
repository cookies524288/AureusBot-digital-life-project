"""网页登录后的习得语言和长期记忆资源。"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from itertools import islice
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.memory import (
    MAX_DETAIL,
    MAX_EXAMPLE,
    MAX_MEMORY_TEXT,
    MAX_TEXT,
    LanguageContent,
    LanguageKind,
    LanguageStore,
    MemoryIndex,
    MemoryKind,
)
from aureus.people import PeopleDirectory

DEFAULT_SIZE = 30
MAX_SIZE = 100
_ATTACHMENT = re.compile(r"attachments/([a-f0-9]{64})")
type RecallFilter = Literal["", "enabled", "disabled"]


class LanguageFields(BaseModel):
    """可由维护者修改的语言内容。"""

    text: str = Field(max_length=MAX_TEXT)
    detail: str = Field(max_length=MAX_DETAIL)
    example: str = Field(default="", max_length=MAX_EXAMPLE)

    def content(self) -> LanguageContent:
        """转换成语言库的公开内容类型。"""
        return LanguageContent(self.text, self.detail, self.example)


class LearningCreate(BaseModel):
    """新增一条手工指定的学习材料。"""

    kind: LanguageKind
    content: LanguageFields


class LearningUpdate(BaseModel):
    """目标条目及其新的正文或使用状态。"""

    kind: LanguageKind
    entry_key: str = Field(min_length=1, max_length=4096)
    content: LanguageFields | None = None
    is_enabled: bool | None = None


class MemoryUpdate(BaseModel):
    """目标记忆及其新的正文或召回设置。"""

    kind: MemoryKind
    entry_id: int = Field(gt=0)
    text: str | None = Field(default=None, max_length=MAX_MEMORY_TEXT)
    is_enabled: bool | None = None


def register_resource_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    language: LanguageStore | None,
    memory: MemoryIndex | None,
    images: InboxImageIndex | None = None,
    people: PeopleDirectory | None = None,
    memory_dir: Path | None = None,
) -> None:
    """注册资源浏览和维护，使用工作线程里的独立数据库连接。"""

    @app.get("/api/resources/media")
    async def browse_media(
        _token: Annotated[str, Depends(require_ready)],
        kind: Literal["emoji", "image_assets"] = "emoji",
        q: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        table = "images" if kind == "emoji" else "image_assets"
        rows = (
            await asyncio.to_thread(
                memory.browse_history,
                q.strip(),
                "",
                offset,
                limit + 1,
                source_table=table,
                omit_image_type="image" if kind == "emoji" else "",
            )
            if memory
            else []
        )
        return {
            "enabled": memory is not None,
            "next_offset": offset + limit if len(rows) > limit else None,
            "items": [
                {
                    "text": row.text,
                    "source": asdict(row.source),
                    "status": row.status,
                    "digest": next(
                        (
                            match[1]
                            for item in row.evidence
                            if item.kind == "attachment_archived"
                            and (match := _ATTACHMENT.fullmatch(item.reference))
                        ),
                        None,
                    ),
                    "media_kind": next(
                        (item.reference for item in row.evidence if item.kind == "media_type"), ""
                    ),
                }
                for row in rows[:limit]
            ],
        }

    @app.get("/api/resources/media/{digest}")
    async def read_media(
        digest: str,
        _token: Annotated[str, Depends(require_ready)],
    ) -> Response:
        if memory_dir is None or _ATTACHMENT.fullmatch(f"attachments/{digest}") is None:
            raise HTTPException(404, "找不到归档图片。")
        directory = (memory_dir / "attachments").resolve()
        path = (directory / digest).resolve()
        if path.parent != directory or not directory.is_relative_to(memory_dir.resolve()):
            raise HTTPException(404, "找不到归档图片。")
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise HTTPException(404, "找不到归档图片。") from None
        media_type = _archive_image_type(data)
        if media_type is None:
            raise HTTPException(415, "归档附件无法作为图片预览。")
        return Response(data, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/resources/profiles")
    async def browse_profiles(
        _token: Annotated[str, Depends(require_ready)],
        q: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        rows = (
            await asyncio.to_thread(people.browse_profiles, q.strip(), offset, limit + 1)
            if people
            else []
        )
        return {
            "enabled": people is not None,
            "items": [asdict(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @app.get("/api/resources/images")
    async def browse_images(
        _token: Annotated[str, Depends(require_ready)],
        before: Annotated[int | None, Query(gt=0)] = None,
        similar_to: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        if images is None:
            return {"enabled": False, "items": [], "next_before": None}
        if similar_to is not None:
            matches = await asyncio.to_thread(images.similar, similar_to, limit)
            if matches is None:
                raise HTTPException(404, "这张图片没有索引。")
            return {"enabled": True, "items": matches, "next_before": None}
        rows = await asyncio.to_thread(images.browse, before, limit + 1)
        return {
            "enabled": True,
            "items": rows[:limit],
            "next_before": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @app.get("/api/resources/learning")
    async def browse_learning(
        _token: Annotated[str, Depends(require_ready)],
        kind: LanguageKind = LanguageKind.EXPRESSION,
        recall: RecallFilter = "",
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
                for entry in language.entries(kind, include_disabled=True)
                if (not recall or entry.is_enabled == (recall == "enabled"))
                and (
                    not term
                    or any(
                        term in field.casefold()
                        for field in (
                            entry.text,
                            entry.detail,
                            entry.example,
                            *(entry.original or ()),
                        )
                    )
                )
            )
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
        kind: MemoryKind = "history",
        recall: RecallFilter = "",
        source_table: Annotated[str, Query(max_length=100)] = "",
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
                return [
                    asdict(row)
                    for row in memory.browse_live(q.strip(), offset, limit + 1, recall=recall)
                ]
            return [
                asdict(row)
                for row in memory.browse_history(
                    q.strip(), status, offset, limit + 1, recall=recall, source_table=source_table
                )
            ]

        rows = await asyncio.to_thread(query)
        counts = (
            await asyncio.to_thread(memory.count_history_sources)
            if memory and kind == "history" and offset == 0
            else {}
        )
        return {
            "enabled": memory is not None,
            "counts": counts,
            "items": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @app.post("/api/resources/learning", status_code=201)
    async def add_learning(
        body: LearningCreate,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, str]:
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        try:
            key = await asyncio.to_thread(language.add_entry, body.kind, body.content.content())
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"entry_key": key}

    @app.patch("/api/resources/learning")
    async def update_learning(
        body: LearningUpdate,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, bool]:
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        if body.content is None and body.is_enabled is None:
            raise HTTPException(422, "请提供修订内容或使用状态。")
        content = body.content.content() if body.content is not None else None
        try:
            found = await asyncio.to_thread(
                language.update_entry, body.kind, body.entry_key, content, body.is_enabled
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if not found:
            raise HTTPException(404, "找不到这条学习记录。")
        return {"ok": True}

    @app.patch("/api/resources/memory")
    async def update_memory(
        body: MemoryUpdate,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, bool]:
        if memory is None:
            raise HTTPException(409, "长期记忆功能当前关闭。")
        if body.text is None and body.is_enabled is None:
            raise HTTPException(422, "请提供修订正文或召回状态。")
        try:
            found = await asyncio.to_thread(
                memory.update_resource, body.kind, body.entry_id, body.text, body.is_enabled
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if not found:
            raise HTTPException(404, "找不到这条长期记忆。")
        return {"ok": True}


def _archive_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None
