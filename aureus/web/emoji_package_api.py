"""表情素材集的选择导出、内容预检和确认登记。"""

import asyncio
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from aureus.memory import EmojiStore
from aureus.memory.emoji_package import (
    PACKAGE_BYTES,
    PACKAGE_MEMBERS,
    SOURCE_LICENSE_CHARS,
    make_emoji_package,
    read_emoji_package,
)

_PATH = "/api/resources/emoji/materials"


class _ExportSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    keys: list[Annotated[str, Field(pattern=r"^asset:[a-f0-9]{64}$")]] = Field(
        min_length=1, max_length=PACKAGE_MEMBERS
    )
    source_license: str = Field(min_length=1, max_length=SOURCE_LICENSE_CHARS)


def register_emoji_package_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool] | None,
    emojis: EmojiStore | None,
) -> None:
    """仅以本机认证、当前网页和明确选择的资产为入口。"""

    def require_store(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> EmojiStore:
        if same_origin is None or not same_origin(request):
            raise HTTPException(403, "请在当前网页处理素材集。")
        if emojis is None:
            raise HTTPException(409, "表情库当前关闭。")
        return emojis

    @app.post(_PATH + "/export")
    async def export_package(
        body: _ExportSelection,
        store: Annotated[EmojiStore, Depends(require_store)],
    ) -> Response:
        def export() -> bytes:
            return make_emoji_package(store.export_materials(body.keys, body.source_license))

        try:
            data = await asyncio.to_thread(export)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return Response(
            data,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="aureus-emoji-materials.json"',
            },
        )

    @app.post(_PATH + "/import")
    async def import_package(
        request: Request,
        store: Annotated[EmojiStore, Depends(require_store)],
        register: bool = False,
        confirmed: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
    ) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(415, "请选择 Aureus v1 JSON 素材集。")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > PACKAGE_BYTES:
                raise HTTPException(413, "素材集超过 32 MiB。")
            raw.extend(chunk)
        digest = sha256(bytes([register]) + raw).hexdigest()
        if confirmed is not None and confirmed != digest:
            raise HTTPException(409, "文件或登记选项与预检不一致，请重新预检。")

        def install() -> dict[str, Any]:
            members = read_emoji_package(bytes(raw))
            counts = store.import_materials(
                members, confirmed=confirmed is not None, register=register
            )
            return {
                "digest": digest,
                **counts,
                "members": [
                    {
                        "digest": item.digest,
                        "description": item.description,
                        "source_license": item.source_license,
                        "bytes": len(item.data),
                    }
                    for item in members
                ],
            }

        try:
            return await asyncio.to_thread(install)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except OSError:
            raise HTTPException(409, "无法保存素材，未登记该素材集。") from None
