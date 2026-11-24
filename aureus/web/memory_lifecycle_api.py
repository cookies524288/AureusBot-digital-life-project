"""经过认证、同源及版本复核的记忆检修接口。"""

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from aureus.memory import (
    MAX_MEMORY_TEXT,
    LifecycleAction,
    LifecycleConflictError,
    MemoryIndex,
    MemorySource,
    SummaryScope,
)
from aureus.people import PeopleDirectory
from aureus.people.profile_maintenance import ProfileChangedError, ProfileEdit, ProfileSource


class LifecyclePreview(BaseModel):
    """指定完整原件键与拟进行的单项操作。"""

    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=4096)
    action: LifecycleAction
    text: str | None = Field(default=None, max_length=MAX_MEMORY_TEXT)


class ProfilePreview(BaseModel):
    """完整画像原件键及本机拟议值。"""

    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=4096)
    edit: ProfileEdit


def register_memory_lifecycle_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    memory: MemoryIndex | None,
    same_origin: Callable[[Request], bool],
    active_session: Callable[[str], bool],
    can_show_summary: Callable[[SummaryScope], bool],
) -> None:
    """注册只按服务端当前权限及完整原件键操作的检修入口。"""
    path = "/api/resources/memory/maintenance"

    def ready(request: Request) -> MemoryIndex:
        if not same_origin(request):
            raise HTTPException(403, "请在当前网页检修记忆。")
        if memory is None:
            raise HTTPException(409, "长期记忆当前关闭。")
        return memory

    def decode_source(source: str) -> MemorySource:
        try:
            return MemorySource.decode(source)
        except (TypeError, ValueError, RecursionError):
            raise HTTPException(404, "未找到对应资料。") from None

    @app.get(path + "/history")
    async def history(
        response: Response,
        token: Annotated[str, Depends(require_ready)],
        source: Annotated[str, Query(min_length=1, max_length=4096)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        location = decode_source(source)
        result = (
            await asyncio.to_thread(
                memory.lifecycle_history,
                location,
                can_show_summary,
                lambda: active_session(token),
                offset,
                limit,
            )
            if memory is not None
            else None
        )
        if result is None:
            raise HTTPException(404, "未找到对应资料。")
        items, total = result
        response.headers["Cache-Control"] = "no-store"
        return {"items": [asdict(item) for item in items], "total": total}

    @app.post(path + "/preview")
    async def preview(
        request: Request,
        response: Response,
        body: LifecyclePreview,
        token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, object]:
        index = ready(request)
        source = decode_source(body.source)
        try:
            plan = await asyncio.to_thread(
                index.preview_lifecycle,
                source,
                body.action,
                body.text,
                can_show_summary,
                lambda: active_session(token),
            )
        except LifecycleConflictError as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法预览记忆检修。") from None
        if plan is None:
            raise HTTPException(404, "未找到对应资料。")
        response.headers["Cache-Control"] = "no-store"
        return plan

    @app.post(path + "/{plan}/{operation}")
    async def change(
        request: Request,
        response: Response,
        plan: str,
        operation: str,
        token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, object]:
        index = ready(request)
        if operation not in ("apply", "undo"):
            raise HTTPException(404, "未找到可执行的检修计划。")
        try:
            result = await asyncio.to_thread(
                index.apply_lifecycle,
                plan,
                can_show_summary,
                lambda: active_session(token),
                undo=operation == "undo",
            )
        except LifecycleConflictError as error:
            raise HTTPException(409, str(error)) from None
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法维护长期记忆，没有修改。") from None
        if result is None:
            raise HTTPException(404, "未找到可执行的检修计划。")
        response.headers["Cache-Control"] = "no-store"
        return result


def register_profile_lifecycle_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    people: PeopleDirectory | None,
    same_origin: Callable[[Request], bool],
    active_session: Callable[[str], bool],
) -> None:
    """只在真实已绑定的画像来源组上读写本机覆盖。"""
    path = "/api/resources/profiles/maintenance"

    def location(value: str) -> ProfileSource:
        try:
            instance, kind, source_key = json.loads(value)
            if not all(isinstance(item, str) and item for item in (instance, kind, source_key)):
                raise ValueError
            return ProfileSource(instance, kind, source_key)
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(404, "未找到可信画像来源。") from None

    def ready(request: Request) -> PeopleDirectory:
        if not same_origin(request):
            raise HTTPException(403, "请在当前网页检修画像。")
        if people is None:
            raise HTTPException(409, "人物资料当前关闭。")
        return people

    @app.get(path)
    async def details(
        response: Response,
        token: Annotated[str, Depends(require_ready)],
        source: Annotated[str, Query(min_length=1, max_length=4096)],
    ) -> dict[str, object]:
        try:
            result = (
                await asyncio.to_thread(
                    people.profile_details,
                    location(source),
                    lambda: active_session(token),
                )
                if people is not None
                else None
            )
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法读取人物画像。") from None
        if result is None:
            raise HTTPException(404, "未找到可信画像来源。")
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.post(path + "/preview")
    async def preview(
        request: Request,
        response: Response,
        body: ProfilePreview,
        token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, object]:
        directory = ready(request)
        try:
            result = await asyncio.to_thread(
                directory.preview_profile,
                location(body.source),
                body.edit,
                lambda: active_session(token),
            )
        except ProfileChangedError as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法预览画像检修。") from None
        if result is None:
            raise HTTPException(404, "未找到可信画像来源。")
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.post(path + "/{plan}/{operation}")
    async def change(
        request: Request,
        response: Response,
        plan: str,
        operation: str,
        token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, bool]:
        directory = ready(request)
        if operation not in ("apply", "undo"):
            raise HTTPException(404, "未找到画像计划。")
        try:
            result = await asyncio.to_thread(
                directory.change_profile,
                plan,
                lambda: active_session(token),
                undo=operation == "undo",
            )
        except ProfileChangedError as error:
            raise HTTPException(409, str(error)) from None
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法维护人物画像，没有修改。") from None
        if not result:
            raise HTTPException(404, "未找到画像计划。")
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True}
