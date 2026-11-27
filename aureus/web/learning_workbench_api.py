"""MaiBot 062d45c 学习工作台的同源资料契约，GPL-3.0。

来源 https://github.com/Mai-with-u/MaiBot/tree/062d45c/src/webui/routers。
使用 Aureus 登录、当前来源授权、权威存储和可逆移除，D109 不提供语言审批门槛。
"""

import asyncio
import json
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile as MultipartFile
from starlette.formparsers import MultiPartException, MultiPartParser

from aureus.common import Clock
from aureus.memory import LanguageKind
from aureus.memory.emoji import MAX_EMOJI_BYTES
from aureus.memory.language import LanguagePackageRow
from aureus.memory.language_package import read_package
from aureus.web.learning_workbench_service import LearningWorkbench
from aureus.web.legacy_expression_import import MAX_LEGACY_BYTES, read_legacy_expressions

_MAX_BATCH_EMOJIS = 20
_MAX_BATCH_EMOJI_BYTES = _MAX_BATCH_EMOJIS * MAX_EMOJI_BYTES + 64 * 1024
_LEGACY_PREVIEW_TTL = timedelta(minutes=15)


class LegacyMapping(BaseModel):
    """旧表达来源与当前目标会话的对应关系。"""

    model_config = ConfigDict(extra="forbid")

    old_chat_id: str = Field(min_length=1, max_length=4096)
    target_chat_id: str | None = Field(default=None, max_length=4096)
    target_chat_ids: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        default_factory=list,
        max_length=100,
    )


class LegacyConfirmation(BaseModel):
    """指定预览批次及确认导入的映射。"""

    model_config = ConfigDict(extra="forbid")

    db_path: str = Field(min_length=1, max_length=64)
    mappings: list[LegacyMapping] = Field(min_length=1, max_length=10000)


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

    ids: list[Annotated[int, Field(gt=0)]] | None = Field(default=None, max_length=100)
    chat_id: str | None = Field(default=None, max_length=4096)
    session_id: str | None = Field(default=None, max_length=4096)
    is_global: bool = False
    include_chat_info: bool = False


class LanguageExport(BaseModel):
    """上游导出请求可选指定 ID；不与批量移除契约共用请求模型。"""

    model_config = ConfigDict(extra="forbid")
    ids: list[Annotated[int, Field(gt=0)]] | None = Field(default=None, max_length=100)
    chat_id: str | None = Field(default=None, max_length=4096)
    is_global: bool = False
    include_chat_info: bool = False


class LanguageImport(BaseModel):
    """MaiBot 表达资源 v1；黑话保留额外元数据字段。"""

    model_config = ConfigDict(extra="forbid")
    chat_id: str | None = None
    session_id: str | None = None
    session_ids: list[str] = Field(default_factory=list, max_length=100)
    target_session_ids: list[str] = Field(default_factory=list, max_length=100)
    is_global: bool = False
    conflict_strategy: Literal["skip", "overwrite"] = "skip"
    expressions: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)
    jargons: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)


class JargonBatch(BaseModel):
    """单独的黑话词义定性。"""

    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=100)
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


class TextCandidateImport(BaseModel):
    """用户在长文预览中勾选的候选。"""

    model_config = ConfigDict(extra="forbid")
    kind: LanguageKind
    targets: list[str] = Field(default_factory=list, max_length=100)
    is_global: bool = False
    candidates: list[dict[str, str]] = Field(min_length=1, max_length=100)


class EmojiBatch(BaseModel):
    """批量目标仅接受同源、当前可管理整数编号。"""

    model_config = ConfigDict(extra="forbid")
    emoji_ids: list[int] = Field(min_length=1, max_length=100)


class EmojiUpload(BaseModel):
    """JSON-less媒体上传的头部字段；原件通过请求流读取。"""

    description: str = Field(default="", max_length=500)
    emotion: str = Field(default="", max_length=1000)
    is_registered: bool = True


def register_learning_workbench_routes(
    app: FastAPI,
    service: LearningWorkbench,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    session_is_active: Callable[[str], bool],
    clock: Clock | None = None,
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
    workbench_clock = clock or service.clock
    previews: dict[str, tuple[str, datetime, dict[str, list[LanguagePackageRow]]]] = {}

    async def run(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except PermissionError as error:
            raise HTTPException(403, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(503, "暂时无法读取或保存学习资料。") from None

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
            if service.language is None:
                raise HTTPException(422, "学习功能当前关闭。")
            targets = set(
                filter(None, (chat_id, session_id, *(chat_ids or []), *(session_ids or [])))
            )
            statuses = {
                "confirmed_jargon": "confirmed",
                "manual_jargon": "confirmed",
                "confirmed_not_jargon": "non_jargon",
                "pending": "candidate",
            }
            offset = (page - 1) * page_size
            if kind == LanguageKind.EXPRESSION:
                if (
                    is_jargon is not None
                    or is_global is not None
                    or is_complete is not None
                    or (jargon_status is not None and jargon_status in statuses)
                ):
                    items, total = [], 0
                else:
                    items, total = await run(
                        service.expression_page,
                        targets,
                        search,
                        offset,
                        page_size,
                        ascending=sort_by.endswith("_asc"),
                        removed=removed,
                    )
                return {
                    "success": True,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "data": items,
                }
            rows = await run(service.language_payloads, kind, removed=removed)
            term = search.casefold()
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
            targets = await run(service.language_chats, kind)
            return {"success": True, "total": len(targets), "data": targets}

        @router.get(path + "/groups")
        async def groups() -> dict[str, Any]:
            return {"success": True, "data": [], "groups": []}

        @router.get(path + "/stats/summary")
        async def stats() -> dict[str, Any]:
            return {"success": True, "data": await run(service.language_statistics, kind)}

        @router.post(path + "/batch/delete")
        async def remove(body: LanguageBatch) -> dict[str, Any]:
            if not body.ids:
                raise HTTPException(422, "请至少选择一条记录。")
            targets = await run(service.language_targets_for_maintenance, kind, body.ids)
            assert service.language is not None
            if not await run(
                service.language.set_removed,
                kind,
                [entry.entry_key for entry in targets],
                True,
                allowed=lambda: service.language_keys_allowed(
                    kind, [entry.entry_key for entry in targets]
                ),
            ):
                raise HTTPException(409, "条目或来源授权已变化，没有修改。")
            return {"success": True, "message": "已可逆移除。", "deleted_count": len(targets)}

        @router.post(path + "/export")
        async def export(body: LanguageExport) -> Response:
            scope = body.chat_id
            if body.ids == []:
                raise HTTPException(422, "请至少选择一条要导出的记录。")
            visible = await run(service.language_payloads, kind)
            if scope is not None and scope not in await run(service.scopes):
                raise HTTPException(422, "所选来源已撤权或不存在。")
            selected = [
                row
                for row in visible
                if (body.ids is None or row["id"] in body.ids)
                and (
                    scope is None
                    or row.get("chat_id") == scope
                    or scope in row.get("session_ids", [])
                )
                and (not body.is_global or (not row.get("chat_id") and not row.get("session_ids")))
            ]
            if body.ids is not None and len(selected) != len(set(body.ids)):
                raise HTTPException(400, "部分记录不存在或不属于当前范围。")
            records = await run(service.export_language, kind, [row["id"] for row in selected])
            timestamp = workbench_clock.now().isoformat()
            if kind == LanguageKind.EXPRESSION:
                package = {
                    "success": True,
                    "version": 1,
                    "type": "maibot.expression.export",
                    "exported_at": timestamp,
                    "source_chat_name": "Aureus",
                    "count": len(records),
                    "expressions": records,
                }
            else:
                package = {
                    "success": True,
                    "version": 1,
                    "type": "maibot.jargon.export",
                    "exported_at": timestamp,
                    "include_chat_info": False,
                    "count": len(records),
                    "jargons": records,
                }
            return Response(
                json.dumps(package, ensure_ascii=False),
                media_type="application/json",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="aureus-{kind}.json"',
                },
            )

        @router.post(path + "/import")
        async def import_entries(
            request: Request,
            token: Annotated[str, Depends(authenticated)],
        ) -> dict[str, Any]:
            raw = await request.body()
            if len(raw) > 8 * 1024 * 1024:
                raise HTTPException(413, "资源包不能超过8 MiB。")
            try:
                body = LanguageImport.model_validate_json(raw)
            except ValidationError:
                raise HTTPException(422, "导入字段格式不符合请求契约。") from None
            scopes = tuple(
                dict.fromkeys(
                    filter(
                        None,
                        (
                            body.chat_id,
                            body.session_id,
                            *body.session_ids,
                            *body.target_session_ids,
                        ),
                    )
                )
            )
            if body.is_global and scopes:
                raise HTTPException(422, "共享导入不能同时指定会话。")
            items = body.expressions if kind == LanguageKind.EXPRESSION else body.jargons
            if not items:
                raise HTTPException(422, "请选择至少一条候选。")
            if kind == LanguageKind.EXPRESSION:
                if body.chat_id and len(scopes) != 1:
                    raise HTTPException(422, "表达方式请选择一个目标会话。")
                package = {
                    "success": True,
                    "version": 1,
                    "type": "maibot.expression.export",
                    "exported_at": workbench_clock.now().isoformat(),
                    "source_chat_name": "Aureus",
                    "count": len(items),
                    "expressions": items,
                }
            else:
                adapted_items = [
                    {**item, "created_by": str(item.get("created_by", "manual")).lower()}
                    for item in items
                ]
                package = {
                    "success": True,
                    "version": 1,
                    "type": "maibot.jargon.export",
                    "exported_at": workbench_clock.now().isoformat(),
                    "include_chat_info": False,
                    "count": len(items),
                    "jargons": adapted_items,
                }
            try:
                rows = read_package(kind, json.dumps(package, ensure_ascii=False).encode())
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            try:
                added = await run(
                    service.import_language,
                    kind,
                    rows,
                    scopes,
                    overwrite=body.conflict_strategy == "overwrite",
                    active=lambda: session_is_active(token),
                )
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            return {
                "success": True,
                "imported_count": added,
                "skipped_count": len(rows) - added,
                "failed_count": 0,
                "message": "导入已完成。",
            }

        @router.post(path + "/clear")
        async def clear(body: dict[str, str]) -> dict[str, Any]:
            scope = body.get("chat_id") or body.get("session_id")
            if not scope or scope not in await run(service.scopes):
                raise HTTPException(422, "请选择当前获准的会话。")
            rows = await run(service.language_payloads, kind)
            identifiers = [
                row["id"]
                for row in rows
                if row.get("chat_id") == scope or scope in row.get("session_ids", [])
            ]
            targets = [
                await run(service.language_target, kind, identifier) for identifier in identifiers
            ]
            assert service.language is not None
            if not await run(
                service.language.set_removed,
                kind,
                [entry.entry_key for entry in targets],
                True,
                allowed=lambda: scope in service.scopes(),
            ):
                raise HTTPException(409, "来源授权已变更；没有修改。")
            return {"success": True, "deleted_count": len(targets), "message": "已可逆移除。"}

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

    @router.post("/expression/legacy-import/preview")
    async def legacy_path_preview() -> dict[str, Any]:
        raise HTTPException(422, "只支持上传旧版数据库，不能访问服务器文件路径。")

    @router.post("/expression/legacy-import/preview-file")
    async def legacy_preview(
        token: Annotated[str, Depends(authenticated)],
        file: Annotated[UploadFile, File(...)],
    ) -> dict[str, Any]:
        data = await file.read(MAX_LEGACY_BYTES + 1)
        if len(data) > MAX_LEGACY_BYTES:
            raise HTTPException(413, "旧版数据库不能超过 32 MiB。")
        try:
            groups = await run(read_legacy_expressions, data)
        except sqlite3.Error:
            raise HTTPException(422, "无法读取上传的旧版表达数据库。") from None
        labels = await run(service.scopes)
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。")
        now = workbench_clock.now()
        for identifier, (owner, deadline, _) in tuple(previews.items()):
            if deadline <= now or owner == token or not session_is_active(owner):
                del previews[identifier]
        if len(previews) >= 4:
            raise HTTPException(429, "其他旧版导入预览尚未完成。")
        identifier = uuid4().hex
        previews[identifier] = (token, now + _LEGACY_PREVIEW_TTL, groups)
        return {
            "success": True,
            "db_path": identifier,
            "total_count": sum(map(len, groups.values())),
            "matched_count": sum(len(rows) for key, rows in groups.items() if key in labels),
            "unmatched_count": sum(len(rows) for key, rows in groups.items() if key not in labels),
            "groups": [
                {
                    "old_chat_id": key,
                    "expression_count": len(rows),
                    "platform": None,
                    "target_id": None,
                    "chat_type": None,
                    "matched_session_id": key if key in labels else None,
                    "matched_chat_name": labels.get(key),
                    "matched": key in labels,
                    "matched_sessions": [{"session_id": key, "chat_name": labels[key]}]
                    if key in labels
                    else [],
                }
                for key, rows in groups.items()
            ],
        }

    @router.post("/expression/legacy-import/import")
    async def legacy_import(
        body: LegacyConfirmation,
        token: Annotated[str, Depends(authenticated)],
    ) -> dict[str, Any]:
        preview = previews.get(body.db_path)
        if preview is None or preview[0] != token or preview[1] <= workbench_clock.now():
            raise HTTPException(409, "旧版预览已失效，请重新上传。")
        if service.language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        saved = preview[2]
        groups = []
        seen = set()
        for mapping in body.mappings:
            key = mapping.old_chat_id
            if key in seen or key not in saved:
                raise HTTPException(422, "所选旧会话不属于当前预览。")
            seen.add(key)
            targets = tuple(
                dict.fromkeys(filter(None, (mapping.target_chat_id, *mapping.target_chat_ids)))
            )
            if targets:
                groups.append((saved[key], targets))
        if not groups:
            raise HTTPException(422, "至少要映射一个当前会话。")
        scopes = {key for _, targets in groups for key in targets}
        previews.pop(body.db_path)
        added = await run(
            service.language.import_workbench_entries,
            LanguageKind.EXPRESSION,
            (),
            (),
            workbench_clock.now(),
            allowed=lambda: session_is_active(token) and scopes <= service.scopes().keys(),
            groups=groups,
        )
        if added is None:
            raise HTTPException(409, "登录或来源已变更；没有写入。")
        return {
            "success": True,
            "message": "旧版表达已导入。",
            "imported_count": added,
            "skipped_count": sum(len(rows) for rows, _ in groups) - added,
            "failed_count": 0,
            "ignored_group_count": len(saved) - len(groups),
        }

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
    async def classify(
        body: JargonBatch,
        token: Annotated[str, Depends(authenticated)],
    ) -> dict[str, Any]:
        changed = await run(
            service.classify_jargons, body.ids, body.is_jargon, lambda: session_is_active(token)
        )
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

    @router.get("/emoji/stats/summary")
    async def emoji_stats() -> dict[str, Any]:
        counts = await run(service.emoji_counts)
        return {
            "success": True,
            "data": {
                "total": counts["total"],
                "registered": counts["registered"],
                "banned": counts["banned"],
                "unregistered": counts["unregistered"],
                "known": counts["registered"] + counts["banned"],
                "unknown": counts["unregistered"],
                "adopted": counts["registered"],
                "discarded": counts["removed"],
                "formats": {},
                "top_used": [],
            },
        }

    @router.post("/emoji/{identifier}/register")
    async def register_emoji(identifier: int) -> dict[str, Any]:
        entry = await run(service.find_emoji, identifier)
        assert service.emojis is not None and entry.digest is not None
        if entry.is_removed:
            raise HTTPException(409, "已移除的表情不能登记。")
        await run(service.emojis.update_catalog, entry.digest, is_registered=True, is_banned=False)
        return {**await emoji(identifier), "message": "表情包已登记。"}

    @router.post("/emoji/{identifier}/ban")
    async def ban_emoji(identifier: int) -> dict[str, Any]:
        entry = await run(service.find_emoji, identifier)
        assert service.emojis is not None and entry.digest is not None
        await run(service.emojis.update_catalog, entry.digest, is_registered=False, is_banned=True)
        return {**await emoji(identifier), "message": "表情包已封禁。"}

    @router.post("/emoji/batch/delete")
    async def batch_delete_emoji(body: EmojiBatch) -> dict[str, Any]:
        if not await run(service.set_emoji_removed, body.emoji_ids, True):
            raise HTTPException(409, "所选表情已变化，没有修改。")
        return {
            "success": True,
            "message": "已可逆移除。",
            "deleted_count": len(body.emoji_ids),
            "failed_count": 0,
            "failed_ids": [],
        }

    @router.delete("/emoji/{identifier}")
    async def delete_emoji(identifier: int) -> dict[str, Any]:
        if identifier <= 0:
            raise HTTPException(422, "请选择有效的表情编号。")
        return await batch_delete_emoji(EmojiBatch(emoji_ids=[identifier]))

    @router.post("/emoji/upload")
    async def upload_emoji(
        token: Annotated[str, Depends(authenticated)],
        file: Annotated[UploadFile, File(...)],
        description: Annotated[str, Form(...)],
    ) -> dict[str, Any]:
        try:
            data = await file.read(MAX_EMOJI_BYTES + 1)
            if len(data) > MAX_EMOJI_BYTES:
                raise HTTPException(413, f"图片不能超过 {MAX_EMOJI_BYTES // (1024 * 1024)} MiB。")
        except HTTPException:
            raise
        except (OSError, ValueError):
            raise HTTPException(422, "无法读取表情上传文件。") from None
        if not file.filename or not description.strip():
            raise HTTPException(422, "请选择有效图片并填写情感标签。")
        tags = tuple(tag.strip() for tag in description.split(",") if tag.strip())
        if service.emojis is None:
            raise HTTPException(409, "表情库当前关闭。")
        digest, created = await run(
            service.emojis.upload,
            bytes(data),
            registration_description=description,
            tags=tags,
            is_registered=True,
            allowed=lambda: session_is_active(token),
        )
        return {
            "success": True,
            "digest": digest,
            "created": created,
            "registered": await run(service.emojis.is_registered, digest),
        }

    @router.post("/emoji/batch/upload")
    async def batch_upload_emoji(
        request: Request,
        token: Annotated[str, Depends(authenticated)],
    ) -> dict[str, Any]:
        if service.emojis is None:
            raise HTTPException(409, "表情库当前关闭。")
        if (
            request.headers.get("content-type", "").split(";", 1)[0].lower()
            != "multipart/form-data"
        ):
            raise HTTPException(422, "请使用批量表情上传表单。")
        too_large = False

        async def bounded_body() -> AsyncGenerator[bytes, None]:
            nonlocal too_large
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > _MAX_BATCH_EMOJI_BYTES:
                    too_large = True
                    raise MultiPartException("批量表情文件超过总大小上限。")
                yield chunk

        try:
            form = await MultiPartParser(
                request.headers,
                bounded_body(),
                max_files=_MAX_BATCH_EMOJIS,
                max_fields=2,
            ).parse()
        except MultiPartException as error:
            raise HTTPException(413 if too_large else 422, error.message) from None
        try:
            files = form.getlist("files")
            if (
                not files
                or any(not isinstance(file, MultipartFile) for file in files)
                or not set(form.keys()) <= {"files", "emotion", "is_registered"}
            ):
                raise HTTPException(422, "请选择批量上传的表情图片。")
            emotion = form.get("emotion", "")
            registered_value = form.get("is_registered", "true")
            if not isinstance(emotion, str) or registered_value not in ("true", "false", "1", "0"):
                raise HTTPException(422, "上传的登记状态或情感标签无效。")
            registered = registered_value in ("true", "1")
            tags = tuple(part.strip() for part in emotion.split(",") if part.strip())
            details = []
            for file in files:
                assert isinstance(file, MultipartFile)
                if not session_is_active(token):
                    raise HTTPException(401, "请先登录。")
                filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
                try:
                    data = await file.read(MAX_EMOJI_BYTES + 1)
                    if len(data) > MAX_EMOJI_BYTES:
                        raise ValueError(f"图片不能超过 {MAX_EMOJI_BYTES // (1024 * 1024)} MiB。")
                    digest, created = await asyncio.to_thread(
                        service.emojis.upload,
                        data,
                        registration_description=emotion,
                        tags=tags,
                        is_registered=registered,
                        allowed=lambda: session_is_active(token),
                    )
                except PermissionError:
                    raise HTTPException(401, "请先登录。") from None
                except ValueError as error:
                    details.append({"filename": filename, "success": False, "error": str(error)})
                    continue
                except OSError:
                    details.append(
                        {"filename": filename, "success": False, "error": "无法保存图片。"}
                    )
                    continue
                key = f"asset:{digest}"
                identifiers = await run(service.emojis.workbench_ids, (key,))
                details.append(
                    {
                        "filename": filename,
                        "success": True,
                        "id": identifiers[key],
                        "existed": not created,
                        "registered": await run(service.emojis.is_registered, digest),
                    }
                )
            uploaded = sum(item["success"] for item in details)
            return {
                "success": True,
                "total": len(details),
                "uploaded": uploaded,
                "failed": len(details) - uploaded,
                "details": details,
                "message": f"成功上传 {uploaded} 个，失败 {len(details) - uploaded} 个。",
            }
        finally:
            await form.close()

    @router.get("/emoji/thumbnail-cache/stats")
    async def thumbnail_cache_stats() -> dict[str, Any]:
        if service.emojis is None:
            raise HTTPException(409, "表情库当前关闭。")
        return {"success": True, **await run(service.emojis.thumbnails.statistics)}

    @router.post("/emoji/thumbnail-cache/cleanup")
    async def thumbnail_cache_cleanup() -> dict[str, Any]:
        return await clean_thumbnails(False)

    @router.post("/emoji/thumbnail-cache/preheat")
    async def thumbnail_cache_preheat(
        token: Annotated[str, Depends(authenticated)],
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        if service.emojis is None:
            raise HTTPException(409, "表情库当前关闭。")
        digests = await run(service.emojis.thumbnail_digests)
        generated = skipped = failed = 0
        for digest in sorted(digests)[:limit]:
            if not session_is_active(token):
                raise HTTPException(401, "请先登录。")
            try:
                source = await run(service.emojis.read_catalog_media, digest)
                if source is None:
                    failed += 1
                    continue
                _, created = await asyncio.to_thread(
                    service.emojis.thumbnails.render, digest, source
                )
                generated += created
                skipped += not created
            except (OSError, ValueError):
                failed += 1
        return {
            "success": True,
            "message": "预热结束。",
            "generated_count": generated,
            "skipped_count": skipped,
            "failed_count": failed,
        }

    @router.delete("/emoji/thumbnail-cache/clear")
    async def thumbnail_cache_clear() -> dict[str, Any]:
        return await clean_thumbnails(True)

    async def clean_thumbnails(clear: bool) -> dict[str, Any]:
        if service.emojis is None:
            raise HTTPException(409, "表情库当前关闭。")
        removed, kept = await run(service.emojis.thumbnails.cleanup, clear=clear)
        return {
            "success": True,
            "message": "缓存清理完成。",
            "cleaned_count": removed,
            "kept_count": kept,
        }

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
    async def media(
        identifier: int,
        token: Annotated[str, Depends(authenticated)],
        original: bool = False,
    ) -> Response:
        entry = await run(service.emoji_target, identifier)
        assert service.emojis is not None
        data = await run(service.emojis.read_catalog_media, entry.digest) if entry.digest else None
        if data is None:
            raise HTTPException(404, "原件不存在。")
        media_type = entry.media_type
        if not original:
            assert entry.digest is not None
            data, _ = await run(service.emojis.thumbnails.render, entry.digest, data)
            media_type = "image/webp"
        if not session_is_active(token):
            raise HTTPException(401, "请先登录。")
        await run(service.emoji_target, identifier)
        return Response(data, media_type=media_type, headers={"Cache-Control": "no-store"})

    app.include_router(router)
