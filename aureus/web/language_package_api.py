"""学习页的语言包下载、无写入预览和确认导入。"""

import asyncio
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response

from aureus.common import Clock, SystemClock
from aureus.memory.language import LanguageKind, LanguageStore
from aureus.memory.language_package import MAX_PACKAGE_BYTES, make_package, read_package

_PATH = "/api/resources/learning/package/{kind}"


def register_language_package_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    language: LanguageStore | None,
    clock: Clock | None,
) -> None:
    """包只保存到已有语言库，未知会话永远不默认进入召回。"""
    package_clock = clock or SystemClock()

    @app.get(_PATH)
    async def export_package(
        kind: LanguageKind, _token: Annotated[str, Depends(require_ready)]
    ) -> Response:
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        try:
            timestamp = package_clock.now().isoformat()
            data = await asyncio.to_thread(make_package, language, kind, timestamp)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return Response(
            data,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="aureus-{kind}.json"',
            },
        )

    @app.post(_PATH)
    async def preview_or_import(
        kind: LanguageKind,
        request: Request,
        _token: Annotated[str, Depends(require_ready)],
        confirmed: Annotated[str | None, Query(pattern="^[0-9a-f]{64}$")] = None,
    ) -> dict[str, str | int]:
        if not same_origin(request):
            raise HTTPException(403, "请从当前网页导入语言包。")
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(415, "请选择 MaiBot v1 格式的 JSON 语言包。")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_PACKAGE_BYTES:
                raise HTTPException(413, "语言包最大为 8 MiB。")
            data.extend(chunk)
        digest = sha256(data).hexdigest()
        if confirmed is not None and confirmed != digest:
            raise HTTPException(409, "文件与预览不同，请重新预览。")
        try:
            rows = await asyncio.to_thread(read_package, kind, bytes(data))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if confirmed is None:
            return {"digest": digest, "can_save": len(rows), "can_recall": 0}
        added = await asyncio.to_thread(language.save_package, kind, rows)
        return {"added": added, "skipped": len(rows) - added, "can_recall": 0}
