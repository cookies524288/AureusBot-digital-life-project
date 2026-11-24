"""供维护者核对和确认当前可访问会话的本地历史清理。"""

import asyncio
import math
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aureus.heartbeat.history_retention import CleanupPreview, HistoryRetention
from aureus.heartbeat.inbox_store import ConversationKey

from .auth import MAX_PASSWORD_LENGTH, LoginLockedError, LoginRejectedError, WebAuth
from .inbox_api import _conversation_key

_NO_STORE = {"Cache-Control": "no-store"}
_CLEANUP_PATH = "/api/inbox/history-cleanup"


class _Confirmation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    proposal_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


def register_inbox_retention_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    session_is_active: Callable[[str], bool],
    same_origin: Callable[[Request], bool],
    auth: WebAuth,
    retention: HistoryRetention | None,
    allows: Callable[[ConversationKey], bool] | None,
) -> None:
    """展示已发表提议；必须通过同源和当前密码核验才能确认。"""

    def permitted(key: ConversationKey, token: str) -> bool:
        return (key.platform == "qq" and allows is not None and allows(key)
                and session_is_active(token) and not auth.password_is_default)

    def current_preview(key: ConversationKey) -> CleanupPreview | None:
        preview = retention.pending(key) if retention is not None else None
        if preview is not None and (allows is None or not allows(key)):
            assert retention is not None
            retention.forget(preview)
            return None
        return preview

    @app.get(_CLEANUP_PATH)
    async def read_preview(
        key: Annotated[ConversationKey, Depends(_conversation_key)],
        token: Annotated[str, Depends(require_ready)],
    ) -> JSONResponse:
        preview = current_preview(key)
        if retention is None or not permitted(key, token):
            return JSONResponse({"preview": None}, headers=_NO_STORE)
        return JSONResponse({"preview": {
            **preview.summary(), "conversation": asdict(key),
            "expires_at": preview.expires_at.isoformat(),
        } if preview else None}, headers=_NO_STORE)

    @app.post(_CLEANUP_PATH)
    async def confirm(
        key: Annotated[ConversationKey, Depends(_conversation_key)], body: _Confirmation,
        request: Request, token: Annotated[str, Depends(require_ready)],
    ) -> JSONResponse:
        if not same_origin(request) or retention is None:
            raise HTTPException(403, "当前会话没有历史清理权限。", headers=_NO_STORE)
        preview = current_preview(key)
        if not permitted(key, token):
            raise HTTPException(403, "当前会话没有历史清理权限。", headers=_NO_STORE)
        if (
            preview is None or preview.id != body.proposal_id
            or not (preview.originals or preview.timeline_only)
        ):
            raise HTTPException(409, "没有可确认的预览，请让助手重新预览。", headers=_NO_STORE)
        try:
            auth.confirm_password(body.password, request.client.host if request.client else "")
        except LoginRejectedError:
            raise HTTPException(403, "密码不对。", headers=_NO_STORE) from None
        except LoginLockedError as error:
            raise HTTPException(429, str(error), headers={
                **_NO_STORE,
                "Retry-After": str(max(1, math.ceil(error.retry_after.total_seconds()))),
            }) from None
        try:
            result = await asyncio.to_thread(
                retention.confirm, preview, lambda: permitted(key, token),
            )
        except sqlite3.Error:
            raise HTTPException(
                503, "无法完成历史清理，未修改记录。", headers=_NO_STORE,
            ) from None
        retention.forget(preview)
        if result == "denied":
            raise HTTPException(403, "登录或会话授权已变化。", headers=_NO_STORE)
        if result == "changed":
            raise HTTPException(
                409, "原件、来源或读水位已有变化，请让助手重新预览。", headers=_NO_STORE,
            )
        return JSONResponse({"completed": True, **preview.summary()}, headers=_NO_STORE)
