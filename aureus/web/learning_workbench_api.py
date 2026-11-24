"""MaiBot 062d45c 学习工作台的同源资料契约，GPL-3.0。

来源 https://github.com/Mai-with-u/MaiBot/tree/062d45c/src/webui/routers。
使用 Aureus 登录、当前来源授权、权威存储和可逆移除，D109 不提供语言审批门槛。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from aureus.memory import LanguageKind
from aureus.web.learning_workbench_service import LearningWorkbench


class ExpressionFields(BaseModel):
    """表达正文和创建来源；来源更正由专门的授权流程处理。"""

    model_config = ConfigDict(extra="forbid")
    situation: str | None = Field(default=None, max_length=160)
    style: str | None = Field(default=None, max_length=80)
    chat_id: str | None = Field(default=None, max_length=4096)
    is_global: bool = False


class JargonFields(BaseModel):
    """黑话含义与定性分别维护。"""

    model_config = ConfigDict(extra="forbid")
    content: str | None = Field(default=None, max_length=80)
    meaning: str | None = Field(default=None, max_length=160)
    session_id: str | None = Field(default=None, max_length=4096)
    session_ids: list[str] | None = Field(default=None, max_length=100)
    is_global: bool | None = None
    is_jargon: bool | None = None
    created_by: Literal["AI", "MANUAL"] | None = None


class LanguageBatch(BaseModel):
    """整批目标编号。"""

    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=100)


class JargonBatch(LanguageBatch):
    """单独的黑话词义定性。"""

    is_jargon: bool


class EmojiFields(BaseModel):
    """目录状态与独立审批保持各自接口。"""

    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=500)
    emotion: str | None = Field(default=None, max_length=1000)
    is_registered: bool | None = None
    is_banned: bool | None = None


class EmojiReview(BaseModel):
    """表情右下角审批保留三态中的明确结论。"""

    model_config = ConfigDict(extra="forbid")
    verdict: Literal["approved", "rejected"]


def register_learning_workbench_routes(
    app: FastAPI,
    service: LearningWorkbench,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    session_is_active: Callable[[str], bool],
) -> None:
    """原工作台路径直接服务同一库；所有变更要求同源和有效登录。"""

    async def authenticated(request: Request, token: Annotated[str, Depends(require_ready)]) -> str:
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "请从当前网页访问学习资料。")
        if request.method != "GET" and not same_origin(request):
            raise HTTPException(403, "请从当前网页维护学习资料。")
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。")
        return token

    router = APIRouter(prefix="/api/webui", dependencies=[Depends(authenticated)])

    async def run(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    def register_language(kind: LanguageKind) -> None:
        path = f"/{kind}"

        @router.get(path + "/list")
        async def listing(
            page: Annotated[int, Query(ge=1)] = 1,
            page_size: Annotated[int, Query(ge=1, le=100)] = 20,
            search: Annotated[str, Query(max_length=200)] = "",
            chat_id: str = "",
            session_id: str = "",
            chat_ids: Annotated[list[str] | None, Query()] = None,
            session_ids: Annotated[list[str] | None, Query()] = None,
            sort_by: Literal["time", "time_asc", "created", "created_asc"] = "time",
            jargon_status: str | None = None,
            is_jargon: bool | None = None,
            is_global: bool | None = None,
            is_complete: bool | None = None,
            removed: bool = False,
        ) -> dict[str, Any]:
            rows = await run(service.language_payloads, kind, removed=removed)
            targets = set(
                filter(None, (chat_id, session_id, *(chat_ids or []), *(session_ids or [])))
            )
            term = search.casefold()
            statuses = {
                "confirmed_jargon": "confirmed",
                "manual_jargon": "confirmed",
                "confirmed_not_jargon": "non_jargon",
                "pending": "candidate",
            }
            rows = [
                row
                for row in rows
                if (
                    not targets
                    or targets.intersection(row.get("session_ids", []) or [row.get("chat_id", "")])
                )
                and (
                    not term
                    or term
                    in " ".join(
                        str(row.get(key, ""))
                        for key in ("style", "situation", "content", "meaning")
                    ).casefold()
                )
                and (is_jargon is None or row.get("is_jargon") == is_jargon)
                and (is_global is None or row.get("is_global") == is_global)
                and (is_complete is None or row.get("is_complete") == is_complete)
                and (
                    jargon_status is None or row.get("jargon_status") == statuses.get(jargon_status)
                )
            ]
            rows.sort(key=lambda row: row["id"], reverse=not sort_by.endswith("_asc"))
            offset = (page - 1) * page_size
            return {
                "success": True,
                "total": len(rows),
                "page": page,
                "page_size": page_size,
                "data": rows[offset : offset + page_size],
            }

        @router.get(path + "/chats")
        @router.get(path + "/chat-targets")
        async def chats() -> dict[str, Any]:
            scopes = await run(service.scopes)
            rows = await run(service.language_payloads, kind)
            return {
                "success": True,
                "total": len(scopes),
                "data": [
                    {
                        "chat_id": key,
                        "session_id": key,
                        "chat_name": label,
                        "expression_count": sum(row.get("chat_id") == key for row in rows),
                        "jargon_count": sum(key in row.get("session_ids", []) for row in rows),
                    }
                    for key, label in scopes.items()
                ],
            }

        @router.get(path + "/groups")
        async def groups() -> dict[str, Any]:
            return {"success": True, "data": [], "groups": []}

        @router.get(path + "/stats/summary")
        async def stats() -> dict[str, Any]:
            rows = await run(service.language_payloads, kind)
            return {
                "success": True,
                "data": {
                    "total": len(rows),
                    "confirmed": sum(row.get("is_jargon", False) for row in rows),
                },
            }

        @router.post(path + "/batch/delete")
        async def remove(body: LanguageBatch) -> dict[str, Any]:
            targets = [await run(service.language_target, kind, item) for item in body.ids]
            assert service.language is not None
            if not await run(
                service.language.set_removed, kind, [entry.entry_key for entry in targets], True
            ):
                raise HTTPException(409, "条目已变化，没有修改。")
            return {"success": True, "message": "已可逆移除。", "deleted_count": len(targets)}

        @router.get(path + "/{identifier}")
        async def detail(identifier: int) -> dict[str, Any]:
            await run(service.language_target, kind, identifier)
            rows = await run(service.language_payloads, kind)
            return {"success": True, "data": next(row for row in rows if row["id"] == identifier)}

        @router.delete(path + "/{identifier}")
        async def delete(identifier: int) -> dict[str, Any]:
            return await remove(LanguageBatch(ids=[identifier]))

    for kind in LanguageKind:
        register_language(kind)

    @router.post("/expression/")
    async def create_expression(body: ExpressionFields) -> dict[str, Any]:
        if body.style is None or body.situation is None:
            raise HTTPException(422, "请填写情境和表达方式。")
        if body.is_global and body.chat_id:
            raise HTTPException(422, "共享创建不能同时指定来源会话。")
        row = await run(
            service.create_language,
            LanguageKind.EXPRESSION,
            body.style,
            body.situation,
            body.chat_id,
        )
        return {"success": True, "message": "已保存表达。", "data": row}

    @router.patch("/expression/{identifier}")
    async def update_expression(identifier: int, body: ExpressionFields) -> dict[str, Any]:
        if body.chat_id is not None or body.is_global:
            raise HTTPException(422, "来源由原件确定，请通过会话授权入口维护。")
        row = await run(
            service.update_language, LanguageKind.EXPRESSION, identifier, body.style, body.situation
        )
        return {"success": True, "message": "已保存修订。", "data": row}

    @router.post("/jargon/")
    async def create_jargon(body: JargonFields) -> dict[str, Any]:
        if body.content is None:
            raise HTTPException(422, "请填写黑话。")
        targets = set(body.session_ids or []) | ({body.session_id} if body.session_id else set())
        if len(targets) > 1 or (body.is_global and targets):
            raise HTTPException(422, "创建时请选择单个来源，跨会话使用沿用当前召回规则。")
        row = await run(
            service.create_language,
            LanguageKind.JARGON,
            body.content,
            body.meaning or "",
            next(iter(targets), None),
        )
        return {"success": True, "message": "已保存黑话。", "data": row}

    @router.patch("/jargon/{identifier}")
    async def update_jargon(identifier: int, body: JargonFields) -> dict[str, Any]:
        if (
            body.session_id is not None
            or body.session_ids is not None
            or body.is_global is not None
        ):
            raise HTTPException(422, "来源由原件确定，请通过会话授权入口维护。")
        if body.is_jargon is not None:
            await run(service.classify_jargons, [identifier], body.is_jargon)
        row = await run(
            service.update_language, LanguageKind.JARGON, identifier, body.content, body.meaning
        )
        return {"success": True, "message": "已保存黑话。", "data": row}

    @router.post("/jargon/batch/set-jargon")
    async def classify(body: JargonBatch) -> dict[str, Any]:
        changed = await run(service.classify_jargons, body.ids, body.is_jargon)
        return {"success": True, "message": "已保存定性。", "changed": changed}

    @router.get("/emoji/list")
    async def emojis(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        search: str = "",
        is_registered: bool | None = None,
        is_banned: bool | None = None,
        status: str | None = None,
        format: str | None = None,
        review: Literal["approved", "rejected", "unreviewed"] | None = None,
        removed: bool = False,
    ) -> dict[str, Any]:
        rows = await run(service.emoji_payloads, removed=removed)
        rows = [
            row
            for row in rows
            if search.casefold() in f"{row['description']} {row['emotion']}".casefold()
            and (is_registered is None or row["is_registered"] == is_registered)
            and (is_banned is None or row["is_banned"] == is_banned)
            and (status is None or row["status"] == status)
            and (format is None or row["format"] == format.lower())
            and (review is None or row["review"] == review)
        ]
        offset = (page - 1) * page_size
        return {
            "success": True,
            "total": len(rows),
            "page": page,
            "page_size": page_size,
            "data": rows[offset : offset + page_size],
        }

    @router.get("/emoji/{identifier}")
    async def emoji(identifier: int) -> dict[str, Any]:
        await run(service.emoji_target, identifier)
        rows = await run(service.emoji_payloads)
        return {"success": True, "data": next(row for row in rows if row["id"] == identifier)}

    @router.patch("/emoji/{identifier}")
    async def update_emoji(identifier: int, body: EmojiFields) -> dict[str, Any]:
        entry = await run(service.emoji_target, identifier)
        if entry.digest is None:
            raise HTTPException(409, "该表情缺少原件。")
        assert service.emojis is not None
        tags = (
            tuple(part.strip() for part in body.emotion.split(",") if part.strip())
            if body.emotion is not None
            else None
        )
        await run(
            service.emojis.update_catalog,
            entry.digest,
            description=body.description,
            tags=tags,
            is_registered=body.is_registered,
            is_banned=body.is_banned,
        )
        return {**await emoji(identifier), "message": "已保存表情。"}

    @router.patch("/emoji/{identifier}/review")
    async def review_emoji(identifier: int, body: EmojiReview) -> dict[str, Any]:
        entry = await run(service.emoji_target, identifier)
        if entry.digest is None:
            raise HTTPException(409, "该表情缺少原件。")
        assert service.emojis is not None
        if not await run(service.emojis.review_catalog, entry.digest, body.verdict):
            raise HTTPException(409, "表情已变化。")
        return {**await emoji(identifier), "message": "已保存独立审批。"}

    @router.get("/emoji/{identifier}/thumbnail")
    async def media(identifier: int, original: bool = False) -> Response:
        entry = await run(service.emoji_target, identifier)
        assert service.emojis is not None
        data = await run(service.emojis.read_catalog_media, entry.digest) if entry.digest else None
        if data is None:
            raise HTTPException(404, "原件不存在。")
        return Response(data, media_type=entry.media_type, headers={"Cache-Control": "no-store"})

    app.include_router(router)
