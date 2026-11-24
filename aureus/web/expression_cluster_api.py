"""学习页：以独立网页授权只读展示已保存的表达簇。"""

import asyncio
import math
import re
import sqlite3
from collections.abc import Awaitable, Callable, Collection
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from aureus.memory import (
    ExpressionClusterStore,
    ExpressionVectorIndex,
    LanguageStore,
    VisibleExpressionCluster,
)

from .auth import MAX_PASSWORD_LENGTH, LoginLockedError, LoginRejectedError, WebAuth
from .expression_cluster_grants import ExpressionClusterDisplay
from .resource_api import DEFAULT_SIZE, MAX_SIZE

type ExpressionClusterGrants = Callable[[str], Collection[str]]

_PATH = "/api/resources/learning/clusters"
_LOCATION = re.compile(r"[0-9a-f]{64}")
_NO_STORE = {"Cache-Control": "no-store"}
_MISSING = "找不到这个表达簇。"
type DisplayScopes = Callable[[], Awaitable[frozenset[str]]]


class ClusterGrant(BaseModel):
    """一条明确选中的表达及其预览版本；空版本表示撤权。"""

    model_config = ConfigDict(extra="forbid")

    entry_key: str = Field(min_length=1, max_length=4096)
    version: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    password: SecretStr = Field(max_length=MAX_PASSWORD_LENGTH)


def register_expression_cluster_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    language: LanguageStore | None,
    store: ExpressionClusterStore | ExpressionVectorIndex | None,
    grants: ExpressionClusterGrants | None,
    session_is_active: Callable[[str], bool],
    *,
    display: ExpressionClusterDisplay | None = None,
    scopes: DisplayScopes | None = None,
) -> None:
    """只读取已注入的快照及当前会话的网页授权，默认没有覆盖。"""

    async def allowed_entries(token: str) -> dict[str, str]:
        if display is not None and scopes is not None:
            return await asyncio.to_thread(display.access, await scopes())
        return dict.fromkeys(grants(token), "") if grants is not None else {}

    async def browse(token: str, *, detail: bool = False) -> tuple[VisibleExpressionCluster, ...]:
        if language is None or store is None:
            return ()
        try:
            allowed = await allowed_entries(token)
            if not allowed:
                return ()
            if isinstance(store, ExpressionVectorIndex) and display is not None:
                clusters = await asyncio.to_thread(
                    store.browse, language, allowed_entry_keys=allowed, authorized_versions=allowed
                )
            else:
                clusters = await asyncio.to_thread(
                    store.browse, language, allowed_entry_keys=allowed
                )
            current = await allowed_entries(token)
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法读取表达簇。", headers=_NO_STORE) from None
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        if allowed != current:
            if detail:
                return ()
            raise HTTPException(409, "表达簇可见范围已变更，请刷新。", headers=_NO_STORE)
        return clusters

    @app.get(_PATH)
    async def read_clusters(
        token: Annotated[str, Depends(require_ready)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> JSONResponse:
        clusters = await browse(token)
        return JSONResponse(
            {
                "items": [
                    {
                        "location": cluster.location,
                        "number": number,
                        "count": len(cluster.members),
                        "title": cluster.members[0].text,
                    }
                    for number, cluster in enumerate(clusters[offset : offset + limit], offset + 1)
                ],
                "total": len(clusters),
                "next_offset": offset + limit if offset + limit < len(clusters) else None,
            },
            headers=_NO_STORE,
        )

    @app.get(_PATH + "/detail")
    async def read_detail(
        token: Annotated[str, Depends(require_ready)],
        location: str = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> JSONResponse:
        if _LOCATION.fullmatch(location):
            for cluster in await browse(token, detail=True):
                if cluster.location == location:
                    total = len(cluster.members)
                    return JSONResponse(
                        {
                            "members": [
                                asdict(member)
                                for member in cluster.members[offset : offset + limit]
                            ],
                            "total": total,
                            "offset": offset,
                            "limit": limit,
                            "next_offset": offset + limit if offset + limit < total else None,
                        },
                        headers=_NO_STORE,
                    )
        raise HTTPException(404, _MISSING, headers=_NO_STORE)


def register_expression_cluster_grants(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    session_is_active: Callable[[str], bool],
    same_origin: Callable[[Request], bool],
    auth: WebAuth,
    display: ExpressionClusterDisplay | None,
    scopes: DisplayScopes | None,
) -> None:
    """授权只接受已登录管理员在表达列表中明确选择的原件。"""

    def active(token: str) -> bool:
        return session_is_active(token) and not auth.password_is_default

    @app.get(_PATH + "/grants")
    async def selection(
        token: Annotated[str, Depends(require_ready)],
        entry_key: Annotated[str, Query(min_length=1, max_length=4096)],
    ) -> JSONResponse:
        if display is None or scopes is None:
            return JSONResponse({"enabled": False}, headers=_NO_STORE)
        try:
            original = await scopes()
            version, saved = await asyncio.to_thread(display.selection, entry_key, original)
            current = await scopes()
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法读取表达展示授权。", headers=_NO_STORE) from None
        if not active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        if not original <= current:
            raise HTTPException(409, "来源范围已变更，请重新选择。", headers=_NO_STORE)
        return JSONResponse(
            {"enabled": True, "version": version, "granted": saved}, headers=_NO_STORE
        )

    @app.put(_PATH + "/grants")
    async def update(
        request: Request, token: Annotated[str, Depends(require_ready)]
    ) -> JSONResponse:
        if not same_origin(request):
            raise HTTPException(403, "请在当前网页操作。", headers=_NO_STORE)
        if display is None or scopes is None:
            raise HTTPException(503, "没有接入表达簇展示授权。", headers=_NO_STORE)
        try:
            body = ClusterGrant.model_validate(await request.json())
        except (ValidationError, ValueError, RecursionError):
            raise HTTPException(422, "请选择表达并确认密码。", headers=_NO_STORE) from None
        if not active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        try:
            auth.confirm_password(
                body.password.get_secret_value(), request.client.host if request.client else ""
            )
        except LoginRejectedError:
            raise HTTPException(403, "密码不对。", headers=_NO_STORE) from None
        except LoginLockedError as error:
            seconds = max(1, math.ceil(error.retry_after.total_seconds()))
            raise HTTPException(
                429, str(error), headers={**_NO_STORE, "Retry-After": str(seconds)}
            ) from None
        try:
            allowed = await scopes() if body.version is not None else frozenset()
            applied = await asyncio.to_thread(
                display.set_grant, body.entry_key, body.version, allowed, lambda: active(token)
            )
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法保存表达展示授权。", headers=_NO_STORE) from None
        if not active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        if not applied:
            raise HTTPException(409, "表达版本或来源已变更，请重新选择。", headers=_NO_STORE)
        return JSONResponse({}, headers=_NO_STORE)
