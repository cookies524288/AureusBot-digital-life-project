"""网页登录后浏览记忆流、下载、检视和追加导入长期记忆。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from tempfile import TemporaryFile
from typing import Annotated, Any, BinaryIO, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import AwareDatetime
from starlette.background import BackgroundTask

from aureus.common import Clock, SystemClock
from aureus.memory import MemoryEventType, MemoryIndex
from aureus.memory.transfer import (
    ARCHIVE_MEDIA_TYPE,
    MemoryArchiveConflictError,
    MemoryArchiveError,
    inspect_archive,
)

from .resource_api import DEFAULT_SIZE, MAX_SIZE

_CHUNK_SIZE = 1024 * 1024
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_FLOW_PATH = "/api/resources/memory/flow"
_TRANSFER_PATH = "/api/resources/memory/transfer"


def _download(file: BinaryIO) -> Iterator[bytes]:
    yield from iter(lambda: file.read(_CHUNK_SIZE), b"")


@asynccontextmanager
async def _read_upload(request: Request) -> AsyncIterator[BinaryIO]:
    if request.headers.get("content-type") != ARCHIVE_MEDIA_TYPE:
        raise HTTPException(415, "请选择 Aureus 导出的 JSONL 记忆包。")
    with cast(BinaryIO, TemporaryFile()) as file:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > _MAX_ARCHIVE_BYTES:
                raise HTTPException(413, "记忆包最大为 2 GiB。")
            await asyncio.to_thread(file.write, chunk)
        file.seek(0)
        try:
            yield file
        except MemoryArchiveConflictError as error:
            raise HTTPException(409, str(error)) from error
        except MemoryArchiveError as error:
            raise HTTPException(422, str(error)) from error


def register_memory_transfer_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    memory: MemoryIndex | None,
    clock: Clock | None = None,
    *,
    require_preview_access: Callable[..., Awaitable[str]],
) -> None:
    """注册记忆流及文件交换，数据库和文件操作交给后台线程。"""
    flow_clock = clock or SystemClock()

    @app.get(_FLOW_PATH)
    async def memory_flow(
        _token: Annotated[str, Depends(require_ready)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
        event_type: MemoryEventType | None = None,
        conversation: Annotated[str, Query(max_length=250)] = "",
        start: AwareDatetime | None = None,
        end: AwareDatetime | None = None,
        hours: Annotated[int | None, Query(ge=1, le=720)] = None,
    ) -> dict[str, Any]:
        if hours is not None:
            if start is not None or end is not None:
                raise HTTPException(422, "请选择快捷范围或指定开始、结束时间。")
            end = flow_clock.now()
            start = end - timedelta(hours=hours)
        if start is not None and end is not None and start > end:
            raise HTTPException(422, "开始时间须早于或等于结束时间。")
        rows, conversations, total = (
            await asyncio.to_thread(
                memory.browse_flow_page,
                offset,
                limit,
                event_type=event_type,
                conversation=conversation,
                start=start,
                end=end,
            )
            if memory
            else ([], [], 0)
        )
        return {
            "enabled": memory is not None,
            "items": [asdict(entry) for entry in rows],
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
            "conversations": conversations,
            "start": start,
            "end": end,
        }

    @app.get(_TRANSFER_PATH)
    async def export_memory(_token: Annotated[str, Depends(require_ready)]) -> StreamingResponse:
        if memory is None:
            raise HTTPException(409, "长期记忆功能当前关闭。")

        def export_file(index: MemoryIndex) -> BinaryIO:
            file = cast(BinaryIO, TemporaryFile())  # noqa: SIM115 - 响应完成时关闭。
            try:
                index.export_archive(file)
                file.seek(0)
            except BaseException:
                file.close()
                raise
            return file

        file = await asyncio.to_thread(export_file, memory)
        return StreamingResponse(
            _download(file),
            media_type=ARCHIVE_MEDIA_TYPE,
            background=BackgroundTask(file.close),
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="aureus-memory.jsonl"',
            },
        )

    @app.post(_TRANSFER_PATH + "/preview")
    async def preview_memory(
        request: Request, _token: Annotated[str, Depends(require_preview_access)]
    ) -> JSONResponse:
        if not same_origin(request):
            raise HTTPException(403, "请从当前网页检视长期记忆包。")
        if memory is None:
            raise HTTPException(409, "长期记忆功能当前关闭。")
        async with _read_upload(request) as file:
            report = await asyncio.to_thread(inspect_archive, file)
        return JSONResponse(report, headers={"Cache-Control": "no-store"})

    @app.post(_TRANSFER_PATH)
    async def import_memory(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, int]:
        if not same_origin(request):
            raise HTTPException(403, "请从当前网页导入长期记忆。")
        if memory is None:
            raise HTTPException(409, "长期记忆功能当前关闭。")
        async with _read_upload(request) as file:
            added, skipped = await asyncio.to_thread(memory.import_archive, file)
        return {"added": added, "skipped": skipped}
