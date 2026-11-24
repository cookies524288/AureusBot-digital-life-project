"""为已登录的维护者提供本地文字索引检修及操作记录。"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from aureus.memory import MemoryIndex
from aureus.timeline import VisibilityScope

from .resource_api import DEFAULT_SIZE, MAX_SIZE

_COVERAGE = (
    "只检查已有文字索引、且时间线原件确认在当前网页可见范围内的本地记录。"
    "未索引、原件缺失或无法确认可见范围的记录不在报告内；"
    "也不检查迁入原件、附件、画像或召回排序。零异常不代表全库完整。"
)
_CHECKS = (
    ("disabled", "维护停用", "维护者停用了这些条目的召回，不需要重建索引。"),
    ("removed", "已移除", "这些条目已移出召回，原件仍保留，不需要重建索引。"),
    ("visibility_mismatch", "可见标签不一致", "索引的可见标签与原件不同；本次不重建索引。"),
)


def register_memory_inspection_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    memory: MemoryIndex | None,
    visibility: VisibilityScope,
) -> None:
    """注册仅查询的检修入口；范围由网页通道决定，不能由请求扩大。"""

    @app.get("/api/resources/memory/inspection")
    async def read_inspection(_token: Annotated[str, Depends(require_ready)]) -> JSONResponse:
        if memory is None:
            raise HTTPException(409, "长期记忆当前未启用，没有执行检修。")
        try:
            result = await asyncio.to_thread(memory.inspect_live, visibility)
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法读取本地文字索引，没有完成检修。") from None
        counts = asdict(result)
        return JSONResponse(
            {
                "coverage": _COVERAGE,
                "checked": result.checked,
                "checks": [
                    {"key": key, "label": label, "detail": detail, "count": counts[key]}
                    for key, label, detail in _CHECKS
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/resources/memory/operations")
    async def read_operations(
        _token: Annotated[str, Depends(require_ready)],
        offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> JSONResponse:
        try:
            result = (
                await asyncio.to_thread(memory.browse_removal_operations, visibility, offset, limit)
                if memory is not None
                else None
            )
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法读取长期记忆操作记录。") from None
        items, total = result if result is not None else ([], 0)
        return JSONResponse(
            jsonable_encoder(
                {
                    "enabled": result is not None,
                    "items": items,
                    "total": total,
                    "next_offset": offset + limit if offset + limit < total else None,
                }
            ),
            headers={"Cache-Control": "no-store"},
        )
