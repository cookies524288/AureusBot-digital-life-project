"""学习工作台：模型质检日志和原范围内的人工挽回。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aureus.common import Clock, SystemClock
from aureus.common.web_identity import WEB_CHANNEL_NAME, WEB_USER_ID
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.language_runner import learning_evidence_available
from aureus.memory import ExpressionQualityAction, ExpressionQualityStatus, LanguageStore
from aureus.timeline import TimelineDatabase

from .resource_api import DEFAULT_SIZE, MAX_SIZE

_PATH = "/api/resources/learning/quality"
_LOCAL_LABELS = {
    json.dumps((WEB_CHANNEL_NAME, WEB_USER_ID), separators=(",", ":")): "网页私聊",
    '["terminal","local"]': "本机终端",
}
_NO_STORE = {"Cache-Control": "no-store"}


class QualityOperation(BaseModel):
    """确认仅引用最近预览的版本，不接受正文或其它目标范围。"""

    model_config = ConfigDict(extra="forbid")

    action: ExpressionQualityAction
    version: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def register_expression_quality_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    session_is_active: Callable[[str], bool],
    language: LanguageStore | None,
    timeline: TimelineDatabase,
    inbox: InboxStore | None,
    allows_inbox: Callable[[ConversationKey], bool] | None,
    clock: Clock | None,
) -> None:
    """读侧按当前范围过滤，确认时核对版本、原件与本次登录是否仍有效。"""
    review_clock = clock or SystemClock()

    def conversations() -> dict[str, str]:
        labels = dict(_LOCAL_LABELS)
        if inbox is not None and allows_inbox is not None:
            for conversation in inbox.list_conversations(visible_to_user=True):
                key = conversation.key
                if allows_inbox(key):
                    label = "群聊" if key.kind == "group" else "私聊"
                    suffix = f" · 来自群 {key.temporary_group_id}" if key.temporary_group_id else ""
                    labels[key.language_scope_key()] = (
                        f"{conversation.name} · {label} {key.recipient_id}"
                        f"（{key.account_id}）{suffix}"
                    )
        return labels

    def source_exists(scope: str, key: str, record_id: int) -> bool:
        return scope in conversations() and learning_evidence_available(
            timeline, inbox, scope, key, record_id
        )

    @app.get(_PATH)
    async def browse(
        token: Annotated[str, Depends(require_ready)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
        scope: Annotated[str, Query(max_length=1000)] = "",
        status: ExpressionQualityStatus | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> JSONResponse:
        if start and end and start > end:
            raise HTTPException(422, "开始日期不能晚于结束日期。", headers=_NO_STORE)
        scopes = await asyncio.to_thread(conversations)
        allowed = tuple(name for name in scopes if not scope or scope == name)
        rows, total = (
            await asyncio.to_thread(
                language.expression_quality_page,
                allowed,
                offset,
                limit,
                status=status,
                start=start,
                end=end,
            )
            if language is not None
            else ([], 0)
        )
        current = await asyncio.to_thread(conversations)
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        if scopes.keys() != current.keys():
            raise HTTPException(409, "来源范围已变更，请刷新。", headers=_NO_STORE)
        return JSONResponse(
            {
                "enabled": language is not None,
                "items": [{**asdict(row), "source_label": scopes[row.scope]} for row in rows],
                "total": total,
                "sources": [{"key": key, "label": label} for key, label in scopes.items()],
            },
            headers=_NO_STORE,
        )

    @app.post(_PATH + "/{identifier}")
    async def maintain(
        identifier: int,
        request: Request,
        body: QualityOperation,
        token: Annotated[str, Depends(require_ready)],
    ) -> JSONResponse:
        if not same_origin(request):
            raise HTTPException(403, "请从当前网页维护质检记录。", headers=_NO_STORE)
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。", headers=_NO_STORE)

        def allowed(scope: str, key: str, record_id: int) -> bool:
            return session_is_active(token) and source_exists(scope, key, record_id)

        result = await asyncio.to_thread(
            language.maintain_expression_quality,
            identifier,
            body.action,
            allowed,
            review_clock.now(),
            version=body.version,
        )
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。", headers=_NO_STORE)
        if result is None:
            raise HTTPException(409, "记录不可操作或已变更，请刷新后重新预览。", headers=_NO_STORE)
        data = {"entry_key": result} if isinstance(result, str) else asdict(result)
        return JSONResponse(data, headers=_NO_STORE)
