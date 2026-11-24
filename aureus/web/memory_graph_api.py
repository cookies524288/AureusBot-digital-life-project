"""把已独立授权的记忆图查询结果转换成网页只读资料。"""

import asyncio
import hmac
import json
import math
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Collection
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from aureus.memory import (
    MemoryGraphNode,
    MemoryGraphPage,
    MemoryGraphRelationDetail,
    MemoryIndex,
    MemorySource,
)

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
) -> None:
    """注册图谱只读入口；没有可信授权提供者时不访问任何图数据。"""

    revision_key = secrets.token_bytes(32)

    def snapshot(token: str) -> tuple[MemorySource, ...]:
        return tuple(grants(token)) if grants is not None else ()

    def scope(sources: Collection[MemorySource]) -> frozenset[str]:
        return frozenset(source.encode() for source in sources)

    def revision_headers(token: str, sources: frozenset[str]) -> dict[str, str]:
        payload = json.dumps([token, sorted(sources)], ensure_ascii=True).encode("ascii")
        return {**_NO_STORE, _REVISION_HEADER: hmac.digest(revision_key, payload, "sha256").hex()}

    def ensure_active(token: str) -> None:
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)

    @app.get("/api/resources/memory/graph/access")
    async def graph_access(token: Annotated[str, Depends(require_ready)]) -> JSONResponse:
        """只核对当前会话与授权范围的版本，不读取记忆。"""
        original = scope(snapshot(token)) if memory is not None else frozenset()
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
        allowed = snapshot(token) if memory is not None else ()
        original = scope(allowed)
        if memory is not None and (neighbor_of is None or anchor is not None):
            try:
                result = await asyncio.to_thread(
                    memory.query_graph,
                    allowed_sources=allowed,
                    query=q,
                    neighbor_of=anchor,
                    offset=offset,
                    limit=limit,
                )
            except sqlite3.Error:
                raise HTTPException(503, _UNAVAILABLE, headers=_NO_STORE) from None
            ensure_active(token)
            if original != scope(snapshot(token)):
                raise HTTPException(409, _PAGE_CHANGED, headers=_NO_STORE)
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
            allowed = snapshot(token)
            original = scope(allowed)
            try:
                result = await asyncio.to_thread(
                    memory.graph_detail, location, allowed_sources=allowed
                )
            except sqlite3.Error:
                raise HTTPException(503, _UNAVAILABLE, headers=_NO_STORE) from None
            ensure_active(token)
            if result is not None and original == scope(snapshot(token)):
                return JSONResponse(_item(result), headers=revision_headers(token, original))
        raise HTTPException(404, _MISSING, headers=_NO_STORE)
