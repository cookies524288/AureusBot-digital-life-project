"""把已独立授权的记忆图查询结果转换成网页只读资料。"""

import asyncio
import hmac
import json
import math
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Collection
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from aureus.memory import (
    GRAPH_CATEGORIES,
    GraphAccess,
    GraphCategory,
    MemoryGraphDisplay,
    MemoryGraphNode,
    MemoryGraphPage,
    MemoryGraphRelationDetail,
    MemoryIndex,
    MemorySource,
)

from .auth import MAX_PASSWORD_LENGTH, LoginLockedError, LoginRejectedError, WebAuth
from .resource_api import DEFAULT_SIZE, MAX_SIZE

type GraphGrants = Callable[[str], Collection[MemorySource]]

_NO_STORE = {"Cache-Control": "no-store"}
_REVISION_HEADER = "X-Aureus-Graph-Revision"
_MISSING = "找不到这条图谱资料。"
_UNAVAILABLE = "暂时无法读取图谱资料。"
_PAGE_CHANGED = "图谱可见范围已变更，请刷新。"


def _location(value: str) -> MemorySource | None:
    try:
        parts = json.loads(value)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parts, list) or len(parts) != 4:
        return None
    instance, role, table, key = parts
    if not all(isinstance(part, str) for part in (instance, role, table)):
        return None
    if not isinstance(key, list) or not all(
        type(part) in (str, int)
        or (type(part) is float and math.isfinite(part))
        or (isinstance(part, dict) and all(isinstance(item, str) for item in part.values()))
        for part in key
    ):
        return None
    return MemorySource(instance, role, table, tuple(key))


def _node(node: MemoryGraphNode) -> dict[str, str]:
    return {"source": node.source.encode(), "name": node.name}


def _item(value: MemoryGraphNode | MemoryGraphRelationDetail) -> dict[str, Any]:
    if isinstance(value, MemoryGraphNode):
        return {
            "kind": "entity",
            **_node(value),
            "evidence": [source.encode() for source in value.evidence],
        }
    return {
        "kind": "relation",
        "source": value.relation.source.encode(),
        "predicate": value.relation.predicate,
        "subject": _node(value.subject),
        "object": _node(value.object),
        "confidence": value.relation.confidence,
        "evidence": [source.encode() for source in value.relation.evidence],
    }


def register_memory_graph_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    memory: MemoryIndex | None,
    grants: GraphGrants | None,
    session_is_active: Callable[[str], bool],
    *,
    display: MemoryGraphDisplay | None = None,
) -> None:
    """注册图谱只读入口；没有可信授权提供者时不访问任何图数据。"""
    revision_key = secrets.token_bytes(32)

    async def snapshot(token: str) -> GraphAccess:
        if memory is not None and display is not None:
            try:
                return await asyncio.to_thread(display.access)
            except sqlite3.Error:
                raise HTTPException(503, _UNAVAILABLE, headers=_NO_STORE) from None
        supplied = grants(token) if grants is not None and memory is not None else ()
        sources = {source.encode(): source for source in supplied}
        return GraphAccess(0, tuple(sources[key] for key in sorted(sources)))

    def revision_headers(token: str, access: GraphAccess) -> dict[str, str]:
        payload = json.dumps(
            [token, access.generation, [source.encode() for source in access.sources]],
            ensure_ascii=True,
        ).encode("ascii")
        return {**_NO_STORE, _REVISION_HEADER: hmac.digest(revision_key, payload, "sha256").hex()}

    def ensure_active(token: str) -> None:
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)

    @app.get("/api/resources/memory/graph/access")
    async def graph_access(token: Annotated[str, Depends(require_ready)]) -> JSONResponse:
        """只核对当前会话与授权范围的版本，不读取记忆。"""
        original = await snapshot(token)
        ensure_active(token)
        return JSONResponse({}, headers=revision_headers(token, original))

    @app.get("/api/resources/memory/graph")
    async def read_graph(
        token: Annotated[str, Depends(require_ready)],
        q: Annotated[str, Query(max_length=200)] = "",
        neighbor_of: str | None = None,
        offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> JSONResponse:
        anchor = _location(neighbor_of) if neighbor_of is not None else None
        result = MemoryGraphPage()
        original = await snapshot(token)
        if memory is not None and (neighbor_of is None or anchor is not None):
            try:
                result = await asyncio.to_thread(
                    memory.query_graph,
                    allowed_sources=original if display is not None else original.sources,
                    query=q,
                    neighbor_of=anchor,
                    offset=offset,
                    limit=limit,
                )
            except sqlite3.Error:
                raise HTTPException(503, _UNAVAILABLE, headers=_NO_STORE) from None
            if original != await snapshot(token):
                raise HTTPException(409, _PAGE_CHANGED, headers=_NO_STORE)
        ensure_active(token)
        return JSONResponse(
            {
                "covered": result.covered,
                "items": [_item(item) for item in result.items],
                "total": result.total,
                "next_offset": result.next_offset,
            },
            headers=revision_headers(token, original),
        )

    @app.get("/api/resources/memory/graph/detail")
    async def read_detail(
        token: Annotated[str, Depends(require_ready)], source: str = ""
    ) -> JSONResponse:
        location = _location(source)
        if memory is not None and location is not None:
            original = await snapshot(token)
            try:
                result = await asyncio.to_thread(
                    memory.graph_detail, location,
                    allowed_sources=original if display is not None else original.sources,
                )
            except sqlite3.Error:
                raise HTTPException(503, _UNAVAILABLE, headers=_NO_STORE) from None
            current = await snapshot(token)
            ensure_active(token)
            if result is not None and original == current:
                return JSONResponse(_item(result), headers=revision_headers(token, original))
        raise HTTPException(404, _MISSING, headers=_NO_STORE)


def register_memory_graph_grant_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    session_is_active: Callable[[str], bool],
    same_origin: Callable[[Request], bool],
    auth: WebAuth,
    display: MemoryGraphDisplay | None,
) -> None:
    """注册独立批次授权；写入前检查同源与当前网页密码。"""

    @app.get("/api/resources/memory/graph/grants")
    async def list_grants(token: Annotated[str, Depends(require_ready)]) -> JSONResponse:
        """只返回批次标识并标注勾选状态，不预览内容。"""
        try:
            batches = await asyncio.to_thread(display.list_batches) if display is not None else []
        except sqlite3.Error:
            raise HTTPException(503, _UNAVAILABLE, headers=_NO_STORE) from None
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        return JSONResponse(
            {"enabled": display is not None, "items": [asdict(batch) for batch in batches]},
            headers=_NO_STORE,
        )

    @app.put("/api/resources/memory/graph/grants/{batch_id}")
    async def set_grants(
        batch_id: str, request: Request, token: Annotated[str, Depends(require_ready)]
    ) -> JSONResponse:
        """整批替换展示类别；空类别撤回本批的全部授权。"""
        if not same_origin(request):
            raise HTTPException(403, "请在当前网页操作。", headers=_NO_STORE)
        if display is None:
            raise HTTPException(503, "没有接入图谱展示授权。", headers=_NO_STORE)
        password, categories = _grant_request(await _read_grant_request(request))
        if not session_is_active(token) or auth.password_is_default:
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        try:
            auth.confirm_password(password, request.client.host if request.client else "")
        except LoginRejectedError:
            raise HTTPException(403, "密码不对。", headers=_NO_STORE) from None
        except LoginLockedError as error:
            seconds = max(1, math.ceil(error.retry_after.total_seconds()))
            raise HTTPException(
                429, str(error), headers={**_NO_STORE, "Retry-After": str(seconds)}
            ) from None
        try:
            applied = await asyncio.to_thread(display.set_categories, batch_id, categories)
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法保存图谱展示授权。", headers=_NO_STORE) from None
        if not applied:
            raise HTTPException(404, "找不到这次发布，请刷新批次列表。", headers=_NO_STORE)
        return JSONResponse({}, headers=_NO_STORE)


async def _read_grant_request(request: Request) -> object:
    try:
        return await request.json()
    except (ValueError, RecursionError):
        raise HTTPException(422, "请选择展示类别并确认密码。", headers=_NO_STORE) from None


def _grant_request(body: object) -> tuple[str, tuple[GraphCategory, ...]]:
    if isinstance(body, dict) and body.keys() == {"password", "categories"}:
        password, categories = body["password"], body["categories"]
        if (
            isinstance(password, str) and len(password) <= MAX_PASSWORD_LENGTH
            and isinstance(categories, list) and len(categories) <= len(GRAPH_CATEGORIES)
            and all(category in GRAPH_CATEGORIES for category in categories)
        ):
            selected = tuple(category for category in GRAPH_CATEGORIES if category in categories)
            return password, selected
    raise HTTPException(422, "请选择展示类别并确认密码。", headers=_NO_STORE)
