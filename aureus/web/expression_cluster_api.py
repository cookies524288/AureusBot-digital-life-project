"""学习页：以独立网页授权只读展示已保存的表达簇。"""

import asyncio
import re
import sqlite3
from collections.abc import Awaitable, Callable, Collection
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from aureus.memory import ExpressionClusterStore, LanguageStore, VisibleExpressionCluster

from .resource_api import DEFAULT_SIZE, MAX_SIZE

type ExpressionClusterGrants = Callable[[str], Collection[str]]

_PATH = "/api/resources/learning/clusters"
_LOCATION = re.compile(r"[0-9a-f]{64}")
_NO_STORE = {"Cache-Control": "no-store"}
_MISSING = "找不到这个表达簇。"


def register_expression_cluster_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    language: LanguageStore | None,
    store: ExpressionClusterStore | None,
    grants: ExpressionClusterGrants | None,
    session_is_active: Callable[[str], bool],
) -> None:
    """只读取已注入的快照及当前会话的网页授权，默认没有覆盖。"""

    async def browse(token: str, *, detail: bool = False) -> tuple[VisibleExpressionCluster, ...]:
        if language is None or store is None or grants is None:
            return ()
        allowed = frozenset(grants(token))
        if not allowed:
            return ()
        try:
            clusters = await asyncio.to_thread(store.browse, language, allowed_entry_keys=allowed)
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法读取表达簇。", headers=_NO_STORE) from None
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        if allowed != frozenset(grants(token)):
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
        token: Annotated[str, Depends(require_ready)], location: str = ""
    ) -> JSONResponse:
        if _LOCATION.fullmatch(location):
            for cluster in await browse(token, detail=True):
                if cluster.location == location:
                    return JSONResponse(
                        {"members": [asdict(member) for member in cluster.members]},
                        headers=_NO_STORE,
                    )
        raise HTTPException(404, _MISSING, headers=_NO_STORE)
