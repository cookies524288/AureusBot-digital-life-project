"""网页登录后浏览记忆流、下载、检视和追加导入长期记忆。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from tempfile import TemporaryFile
from typing import Annotated, Any, BinaryIO, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints
from starlette.background import BackgroundTask

from aureus.common import Clock, SystemClock
from aureus.memory import MemoryEventType, MemoryIndex
from aureus.memory.bundle_format import (
    MAX_BUNDLE_BYTES,
    BundleMode,
    ContentLevel,
    MemoryBundleConflictError,
    MemoryBundleError,
)
from aureus.memory.transfer import (
    ARCHIVE_MEDIA_TYPE,
    MemoryArchiveConflictError,
    MemoryArchiveError,
    inspect_archive,
)
from aureus.people import PeopleDirectory

from .resource_api import DEFAULT_SIZE, MAX_SIZE

_CHUNK_SIZE = 1024 * 1024
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_FLOW_PATH = "/api/resources/memory/flow"
_TRANSFER_PATH = "/api/resources/memory/transfer"
_BUNDLE_PATH = "/api/resources/memory/bundles"
_BUNDLE_MEDIA_TYPE = "application/zip"


class BundleExportSelection(BaseModel):
    """只接受服务端登记的来源编号和规范内容层级。"""

    model_config = ConfigDict(extra="forbid")
    source_ids: list[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]] = Field(
        min_length=1,
        max_length=100,
    )
    content_level: ContentLevel = "knowledge"
    include_images: bool = False
    preview_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


def _download(file: BinaryIO) -> Iterator[bytes]:
    yield from iter(lambda: file.read(_CHUNK_SIZE), b"")


@asynccontextmanager
async def _read_upload(request: Request, *, bundle: bool = False) -> AsyncIterator[BinaryIO]:
    media_type = _BUNDLE_MEDIA_TYPE if bundle else ARCHIVE_MEDIA_TYPE
    if request.headers.get("content-type") != media_type:
        hint = "请选择 .amembundle 记忆包。" if bundle else "请选择 Aureus 导出的 JSONL 记忆包。"
        raise HTTPException(415, hint)
    with cast(BinaryIO, TemporaryFile()) as file:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > (MAX_BUNDLE_BYTES if bundle else _MAX_ARCHIVE_BYTES):
                raise HTTPException(
                    413, "记忆包超过上传大小上限。" if bundle else "记忆包最大为 2 GiB。"
                )
            await asyncio.to_thread(file.write, chunk)
        file.seek(0)
        try:
            yield file
        except (MemoryArchiveConflictError, MemoryBundleConflictError) as error:
            raise HTTPException(409, str(error)) from error
        except (MemoryArchiveError, MemoryBundleError) as error:
            raise HTTPException(422, str(error)) from error


def register_memory_transfer_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    memory: MemoryIndex | None,
    clock: Clock | None = None,
    *,
    require_preview_access: Callable[..., Awaitable[str]],
    people: PeopleDirectory | None = None,
) -> None:
    """注册记忆流及文件交换，数据库和文件操作交给后台线程。"""
    flow_clock = clock or SystemClock()
    _register_bundle_routes(app, require_ready, same_origin, memory, require_preview_access, people)

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


def _register_bundle_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    memory: MemoryIndex | None,
    require_preview_access: Callable[..., Awaitable[str]],
    people: PeopleDirectory | None,
) -> None:
    def index(request: Request | None = None) -> MemoryIndex:
        if request is not None and not same_origin(request):
            raise HTTPException(403, "请从当前网页操作长期记忆包。")
        if memory is None:
            raise HTTPException(409, "长期记忆功能当前关闭。")
        return memory

    @app.post(_BUNDLE_PATH + "/preview")
    async def preview(
        request: Request, _token: Annotated[str, Depends(require_preview_access)]
    ) -> JSONResponse:
        current = index(request)
        async with _read_upload(request, bundle=True) as file:
            report = await asyncio.to_thread(current.preview_bundle, file, people)
        return JSONResponse(report, headers={"Cache-Control": "no-store"})

    @app.post(_BUNDLE_PATH)
    async def install(
        request: Request, _token: Annotated[str, Depends(require_ready)], mode: BundleMode = "merge"
    ) -> JSONResponse:
        current = index(request)
        async with _read_upload(request, bundle=True) as file:
            report = await asyncio.to_thread(current.install_bundle, file, mode, people)
        return JSONResponse(report, headers={"Cache-Control": "no-store"})

    @app.get(_BUNDLE_PATH)
    async def installations(
        _token: Annotated[str, Depends(require_ready)],
        offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> JSONResponse:
        items = await asyncio.to_thread(index().list_bundles, offset, limit + 1)
        return JSONResponse(
            {
                "items": items[:limit],
                "next_offset": offset + limit if len(items) > limit else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post(_BUNDLE_PATH + "/{installation_id}/{action}")
    async def set_installed(
        request: Request,
        _token: Annotated[str, Depends(require_ready)],
        installation_id: str,
        action: Literal["uninstall", "restore"],
    ) -> dict[str, bool]:
        installed = action == "restore"
        if not await asyncio.to_thread(
            index(request).set_bundle_installed, installation_id, installed, people
        ):
            raise HTTPException(404, "找不到该包安装记录。")
        return {"installed": installed}

    @app.get(_BUNDLE_PATH + "/sources")
    async def export_sources(_token: Annotated[str, Depends(require_ready)]) -> JSONResponse:
        sources = await asyncio.to_thread(index().list_bundle_sources)
        return JSONResponse(
            {"items": [asdict(source) for source in sources]}, headers={"Cache-Control": "no-store"}
        )

    @app.post(_BUNDLE_PATH + "/export-preview")
    async def preview_export(
        request: Request,
        selection: BundleExportSelection,
        _token: Annotated[str, Depends(require_ready)],
    ) -> JSONResponse:
        try:
            report = await asyncio.to_thread(
                index(request).generate_bundle,
                selection.source_ids,
                selection.content_level,
                people,
                include_images=selection.include_images,
            )
        except MemoryBundleConflictError as error:
            raise HTTPException(409, str(error)) from error
        return JSONResponse(report, headers={"Cache-Control": "no-store"})

    @app.get(_BUNDLE_PATH + "/export")
    async def legacy_export(_token: Annotated[str, Depends(require_ready)]) -> None:
        raise HTTPException(409, "本库生成需要先选择可信来源并预检。")

    @app.post(_BUNDLE_PATH + "/export")
    async def generate_export(
        request: Request,
        selection: BundleExportSelection,
        _token: Annotated[str, Depends(require_ready)],
    ) -> StreamingResponse:
        current = index(request)

        def build(file: BinaryIO) -> None:
            current.generate_bundle(
                selection.source_ids,
                selection.content_level,
                people,
                output=file,
                expected_digest=selection.preview_digest,
                include_images=selection.include_images,
            )

        return await _stream_bundle(build, "aureus-memory.amembundle")

    @app.get(_BUNDLE_PATH + "/{installation_id}/download")
    async def download(
        _token: Annotated[str, Depends(require_ready)], installation_id: str
    ) -> StreamingResponse:
        current = index()

        def build(file: BinaryIO) -> None:
            if not current.export_bundle(installation_id, file):
                raise HTTPException(404, "找不到该包安装记录。")

        return await _stream_bundle(build, "maibot-memory.amembundle")


async def _stream_bundle(build: Callable[[BinaryIO], None], filename: str) -> StreamingResponse:
    def make_file() -> BinaryIO:
        file = cast(BinaryIO, TemporaryFile())  # noqa: SIM115 - 响应完成时关闭。
        try:
            build(file)
            file.seek(0)
            return file
        except BaseException:
            file.close()
            raise

    try:
        file = await asyncio.to_thread(make_file)
    except MemoryBundleConflictError as error:
        raise HTTPException(409, str(error)) from error
    return StreamingResponse(
        _download(file),
        media_type=_BUNDLE_MEDIA_TYPE,
        background=BackgroundTask(file.close),
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
