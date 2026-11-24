"""学习页：查看实际表达向量空间、显式开始和取消一批补建。"""

import sqlite3
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aureus.core.embedding_progress import EmbeddingProgress
from aureus.heartbeat.expression_recall import EXPRESSION_MAINTENANCE_BATCH, ExpressionRecall

_PATH = "/api/resources/learning/vector-spaces"
_NO_STORE = {"Cache-Control": "no-store"}


class StartBackfill(BaseModel):
    """只接受页面明确选中的空间。"""

    model_config = ConfigDict(extra="forbid")

    space: str = Field(pattern=r"^[a-f0-9]{64}$")


def register_expression_vector_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    session_is_active: Callable[[str], bool],
    progress: EmbeddingProgress | None,
    recall: ExpressionRecall | None,
) -> None:
    """补建不会产生表达簇的网页展示授权。"""

    def available() -> str | None:
        if recall is None or progress is None or not progress.expression_enabled:
            return None
        return recall.space

    def check_session(token: str) -> None:
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)

    @app.get(_PATH)
    async def list_spaces(token: Annotated[str, Depends(require_ready)]) -> JSONResponse:
        active = available()
        try:
            scopes = await progress.expression_scopes() if progress is not None else frozenset()
            rows = await progress.expression_spaces(scopes, active) if progress is not None else []
            current = await progress.expression_scopes() if progress is not None else frozenset()
        except (sqlite3.Error, OSError):
            raise HTTPException(503, "暂时无法读取表达向量空间。", headers=_NO_STORE) from None
        check_session(token)
        if active != available() or not scopes <= current:
            raise HTTPException(409, "模型路线或来源授权已变更，请刷新。", headers=_NO_STORE)
        run = recall.maintenance_run if recall is not None else None
        return JSONResponse(
            {
                "items": [
                    {
                        "space": row.space,
                        "available": row.space == active,
                        "established": row.established,
                        "eligible": row.eligible,
                    }
                    for row in rows
                ],
                "batch_limit": EXPRESSION_MAINTENANCE_BATCH,
                "job": {"id": run.id, "state": run.state, "written": run.written}
                if run is not None and run.owner == token
                else None,
            },
            headers=_NO_STORE,
        )

    @app.post(_PATH + "/backfill", status_code=202)
    async def start(
        body: StartBackfill, request: Request, token: Annotated[str, Depends(require_ready)]
    ) -> JSONResponse:
        if not same_origin(request):
            raise HTTPException(403, "请从当前页面执行补建。", headers=_NO_STORE)
        if progress is None or recall is None or body.space != available():
            raise HTTPException(409, "所选表达向量空间不可用。", headers=_NO_STORE)
        service = progress
        model_binding = service.models.embedding_spaces
        try:
            scopes = await service.expression_scopes()
        except (sqlite3.Error, OSError):
            raise HTTPException(503, "暂时无法核对来源授权。", headers=_NO_STORE) from None
        check_session(token)
        if service.models.embedding_spaces is not model_binding:
            raise HTTPException(409, "模型路线已变更，请刷新。", headers=_NO_STORE)

        async def allows() -> bool:
            current = await service.expression_scopes()
            return (
                scopes <= current
                and session_is_active(token)
                and service.models.embedding_spaces is model_binding
            )

        run = recall.start_maintenance(token, body.space, scopes, allows)
        if run is None:
            raise HTTPException(409, "正在补建或所选空间已变更，请刷新。", headers=_NO_STORE)
        return JSONResponse({"id": run.id}, status_code=202, headers=_NO_STORE)

    @app.delete(_PATH + "/backfill/{job}")
    async def cancel(
        job: str, request: Request, token: Annotated[str, Depends(require_ready)]
    ) -> JSONResponse:
        if not same_origin(request):
            raise HTTPException(403, "请从当前页面取消补建。", headers=_NO_STORE)
        run = recall.maintenance_run if recall is not None else None
        if recall is None or run is None or run.id != job or run.owner != token:
            raise HTTPException(404, "找不到这次补建。", headers=_NO_STORE)
        if not recall.cancel_maintenance(run):
            raise HTTPException(409, "本次补建已提交写入或已结束。", headers=_NO_STORE)
        return JSONResponse({"state": run.state}, headers=_NO_STORE)
