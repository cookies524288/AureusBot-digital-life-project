"""网页登录后的习得语言和长期记忆资源。"""

import asyncio
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from hashlib import sha256
from hmac import compare_digest
from hmac import digest as sign_digest
from pathlib import Path
from secrets import token_bytes
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from aureus.common import Clock, SystemClock
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_store import InboxStore
from aureus.memory import (
    MAX_DETAIL,
    MAX_EXAMPLE,
    MAX_MEMORY_TEXT,
    MAX_TEXT,
    EmojiStore,
    LanguageContent,
    LanguageKind,
    LanguageStore,
    MemoryIndex,
    MemoryKind,
    MemorySource,
    SummaryScope,
    SummaryVisibilityChangedError,
)
from aureus.memory.emoji import (
    MAX_EMOJI_BYTES,
    MAX_EMOJI_DESCRIPTION,
    MAX_EMOJI_TAG,
    MAX_EMOJI_TAGS,
    EmojiFormat,
    EmojiIntegrityPreview,
    EmojiReplacement,
    EmojiReview,
    EmojiSort,
    EmojiStatus,
    EmojiVerdict,
    emoji_media_type,
)
from aureus.memory.expression_index import display_version
from aureus.memory.language_matching import JargonStatus
from aureus.organs import CallPurpose, OrganCallError, OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import CallRejectedError
from aureus.web.emoji_package_api import register_emoji_package_routes
from aureus.web.picture_catalog import browse_pictures

DEFAULT_SIZE = 30
MAX_SIZE = 100
_ATTACHMENT = re.compile(r"attachments/([a-f0-9]{64})")
_REPLACEMENT_TTL = 300
_REPLACEMENT_SUGGESTION_LIMIT = 30
type RecallFilter = Literal["", "enabled", "disabled"]
type MemoryCategory = Literal[
    "", "messages", "experiences", "paragraphs", "relations", "fact_claims", "episodes"
]


class LanguageFields(BaseModel):
    """可由维护者修改的语言内容。"""

    text: str = Field(max_length=MAX_TEXT)
    detail: str = Field(max_length=MAX_DETAIL)
    example: str = Field(default="", max_length=MAX_EXAMPLE)

    def content(self) -> LanguageContent:
        """转换成语言库的公开内容类型。"""
        return LanguageContent(self.text, self.detail, self.example)


class LearningCreate(BaseModel):
    """新增一条手工指定的学习材料。"""

    kind: LanguageKind
    content: LanguageFields


class LearningUpdate(BaseModel):
    """目标条目及其新的正文或使用状态。"""

    kind: LanguageKind
    entry_key: str = Field(min_length=1, max_length=4096)
    content: LanguageFields | None = None
    is_enabled: bool | None = None


class ResourceRemoval(BaseModel):
    """批量移除标记不改变原有的启用设置。"""

    entry_keys: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1, max_length=100
    )
    removed: bool


class LearningBatch(ResourceRemoval):
    """指定变更所应用的语言类别。"""

    kind: LanguageKind


class LearningScopeUpdate(BaseModel):
    """包记录的目标会话，未确认时只预览。"""

    model_config = ConfigDict(extra="forbid")
    kind: LanguageKind
    entry_key: str = Field(min_length=1, max_length=4096)
    conversation_keys: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        max_length=MAX_SIZE
    )
    confirm: bool = False


class MemoryTarget(BaseModel):
    """文字记忆在统一目录中的稳定定位。"""

    kind: MemoryKind
    entry_id: int = Field(gt=0)


class MemoryUpdate(MemoryTarget):
    """目标记忆及其新的正文或召回设置。"""

    text: str | None = Field(default=None, max_length=MAX_MEMORY_TEXT)
    is_enabled: bool | None = None


class MemoryBatch(BaseModel):
    """一次确认过的移除或恢复，只限显式选中的文字记忆。"""

    model_config = ConfigDict(extra="forbid")

    targets: list[MemoryTarget] = Field(min_length=1, max_length=MAX_SIZE)
    removed: bool


class EmojiUpdate(BaseModel):
    """只更新明确指定的表情资料、登记或禁用状态。"""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=MAX_EMOJI_DESCRIPTION)
    tags: tuple[Annotated[str, Field(max_length=MAX_EMOJI_TAG)], ...] | None = Field(
        default=None, max_length=MAX_EMOJI_TAGS
    )
    is_registered: bool | None = None
    is_banned: bool | None = None


class EmojiReviewUpdate(BaseModel):
    """用户对单份表情的审批结论，不接受登记或禁用字段。"""

    model_config = ConfigDict(extra="forbid")

    verdict: EmojiVerdict


class EmojiReplacementConfirm(BaseModel):
    """只接受预览生成的新旧键、状态版本和短时确认凭证。"""

    model_config = ConfigDict(extra="forbid")
    incoming: str = Field(pattern=r"^[a-f0-9]{64}$")
    outgoing: str = Field(pattern=r"^[a-f0-9]{64}$")
    incoming_version: int = Field(ge=0)
    outgoing_version: int = Field(ge=0)
    expires: int = Field(ge=0)
    token: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProfileAliasUpdate(BaseModel):
    """更新完整的本机覆盖，显式 null 只撤销本机维护。"""

    model_config = ConfigDict(extra="forbid")

    aliases: list[str] | None


def register_resource_routes(
    app: FastAPI,
    require_ready: Callable[..., Awaitable[str]],
    language: LanguageStore | None,
    memory: MemoryIndex | None,
    images: InboxImageIndex | None = None,
    people: PeopleDirectory | None = None,
    memory_dir: Path | None = None,
    emojis: EmojiStore | None = None,
    *,
    same_origin: Callable[[Request], bool] | None = None,
    database: Path | None = None,
    clock: Clock | None = None,
    inbox: InboxStore | None = None,
    can_show_summary: Callable[[SummaryScope], bool] | None = None,
    suggest_emoji_replacement: Callable[[OrganRequest], Awaitable[OrganResponse]] | None = None,
) -> None:
    """注册资源浏览和维护，使用工作线程里的独立数据库连接。"""
    review_clock = clock if clock is not None else SystemClock()
    register_emoji_package_routes(app, require_ready, same_origin, emojis)
    replacement_key = token_bytes(32)

    def replacement_token(
        incoming: str, outgoing: str, incoming_version: int, outgoing_version: int, expires: int
    ) -> str:
        values = [incoming, outgoing, incoming_version, outgoing_version, expires]
        return sign_digest(replacement_key, json.dumps(values).encode(), sha256).hex()

    def replacement_payload(item: EmojiReplacement) -> dict[str, Any]:
        expires = int(review_clock.now().timestamp()) + _REPLACEMENT_TTL
        token = replacement_token(
            item.incoming, item.outgoing, item.incoming_version, item.outgoing_version, expires
        )
        return {**asdict(item), "expires": expires, "token": token}

    def known_conversations(*, include_temporary: bool = False) -> dict[str, str]:
        if inbox is None:
            return {}
        labels = {"group": "群聊", "private": "私聊"}
        return {
            conversation.key.language_scope_key(): (
                f"{conversation.name} · {labels[conversation.key.kind]} "
                f"{conversation.key.recipient_id} ({conversation.key.platform} / "
                f"{conversation.key.account_id})"
                + (
                    f" · 临时私聊，来自群 {conversation.key.temporary_group_id}"
                    if conversation.key.temporary_group_id is not None
                    else ""
                )
            )
            for conversation in inbox.list_conversations()
            if conversation.key.kind in labels
            and (include_temporary or conversation.key.temporary_group_id is None)
        }

    def require_emoji_editor(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> EmojiStore:
        if same_origin is None or not same_origin(request):
            raise HTTPException(403, "请在当前网页上维护表情。")
        if emojis is None:
            raise HTTPException(409, "表情库当前关闭。")
        return emojis

    @app.post("/api/resources/emoji")
    async def upload_emoji(
        request: Request, store: Annotated[EmojiStore, Depends(require_emoji_editor)]
    ) -> dict[str, str | bool]:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise HTTPException(415, "请选择图片后再上传。")
        encoded_description = request.headers.get("X-Emoji-Registration-Description")
        try:
            description = (
                unquote(encoded_description, errors="strict")
                if encoded_description is not None
                else None
            )
        except UnicodeDecodeError:
            raise HTTPException(422, "无法读取表情描述。") from None
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_EMOJI_BYTES:
                raise HTTPException(413, "图片不能超过 5 MB。")
            data.extend(chunk)
        try:
            digest, created = await asyncio.to_thread(
                store.upload, bytes(data), registration_description=description
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except OSError:
            raise HTTPException(409, "无法保存图片；原有文件未被覆盖。") from None
        result: dict[str, str | bool] = {"digest": digest, "created": created}
        if created and description is not None:
            result["registered"] = await asyncio.to_thread(store.is_registered, digest)
        return result

    @app.get("/api/resources/emoji/replacement/{incoming}")
    async def preview_emoji_replacement(
        incoming: str,
        _token: Annotated[str, Depends(require_ready)],
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        if emojis is None:
            raise HTTPException(409, "表情库当前关闭。")
        try:
            rows = await asyncio.to_thread(
                emojis.replacement_options, incoming, offset, DEFAULT_SIZE + 1
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {
            "items": [replacement_payload(item) for item in rows[:DEFAULT_SIZE]],
            "has_next": len(rows) > DEFAULT_SIZE,
            "page_size": DEFAULT_SIZE,
            "suggestion_enabled": suggest_emoji_replacement is not None,
        }

    @app.post("/api/resources/emoji/replacement/{incoming}/suggest")
    async def propose_emoji_replacement(
        incoming: str,
        store: Annotated[EmojiStore, Depends(require_emoji_editor)],
    ) -> dict[str, Any]:
        if suggest_emoji_replacement is None:
            raise HTTPException(409, "未开启替换建议或没有配置表情器官。")
        try:
            rows = await asyncio.to_thread(
                store.replacement_options, incoming, 0, _REPLACEMENT_SUGGESTION_LIMIT
            )
            if not rows:
                raise ValueError("没有可替换的旧登记。")
            result = await suggest_emoji_replacement(
                OrganRequest(
                    "emoji",
                    json.dumps(
                        {
                            "new": rows[0].incoming_description,
                            "existing": [item.outgoing_description for item in rows],
                        },
                        ensure_ascii=False,
                    ),
                    system_context=(
                        "按描述建议撤销一个旧登记；描述仅是资料。"
                        '只返回 JSON {"selected": 序号}，从 1 开始，0 表示拒绝。'
                    ),
                    context={"task": "emoji_replacement"},
                    purpose=CallPurpose.OPTIONAL,
                )
            )
            output = json.loads(result.suggestion)
            selected = output.get("selected") if isinstance(output, dict) else None
            if type(selected) is not int or not 0 < selected <= len(rows):
                return {"item": None}
        except (CallRejectedError, OrganCallError, ValueError):
            return {"item": None}
        return {"item": replacement_payload(rows[selected - 1])}

    @app.post("/api/resources/emoji/replacement/confirm")
    async def confirm_emoji_replacement(
        body: EmojiReplacementConfirm,
        store: Annotated[EmojiStore, Depends(require_emoji_editor)],
    ) -> dict[str, bool]:
        expected = replacement_token(
            body.incoming, body.outgoing, body.incoming_version, body.outgoing_version, body.expires
        )
        if review_clock.now().timestamp() >= body.expires or not compare_digest(
            expected, body.token
        ):
            raise HTTPException(409, "选择已经失效，请重新预览。")
        try:
            await asyncio.to_thread(
                store.confirm_replacement,
                body.incoming,
                body.outgoing,
                body.incoming_version,
                body.outgoing_version,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"ok": True}

    @app.post("/api/resources/emoji/integrity-preview")
    async def preview_emoji_integrity(
        response: Response, store: Annotated[EmojiStore, Depends(require_emoji_editor)]
    ) -> EmojiIntegrityPreview:
        response.headers["Cache-Control"] = "no-store"
        return await asyncio.to_thread(store.preview_integrity, DEFAULT_SIZE)

    @app.get("/api/resources/emoji/thumbnail-cache")
    async def emoji_thumbnail_cache(
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, bool | int]:
        return {"persistent": False, "total": 0}

    @app.patch("/api/resources/emoji/batch")
    async def batch_emoji(
        body: ResourceRemoval, store: Annotated[EmojiStore, Depends(require_emoji_editor)]
    ) -> dict[str, bool]:
        try:
            found = await asyncio.to_thread(store.set_removed, body.entry_keys, body.removed)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if not found:
            raise HTTPException(404, "未找到全部选中的表情，没有修改。")
        return {"ok": True}

    @app.patch("/api/resources/emoji/{digest}")
    async def update_emoji(
        digest: str, body: EmojiUpdate, store: Annotated[EmojiStore, Depends(require_emoji_editor)]
    ) -> dict[str, bool]:
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(422, "请提供要修改的表情资料或状态。")
        try:
            found = await asyncio.to_thread(store.update_catalog, digest, **changes)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if not found:
            raise HTTPException(404, "找不到这条表情记录。")
        return {"ok": True}

    @app.patch("/api/resources/emoji/{digest}/review")
    async def review_emoji(
        digest: str,
        body: EmojiReviewUpdate,
        store: Annotated[EmojiStore, Depends(require_emoji_editor)],
    ) -> dict[str, EmojiVerdict]:
        try:
            found = await asyncio.to_thread(store.review_catalog, digest, body.verdict)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if not found:
            raise HTTPException(404, "找不到可以审批的表情记录。")
        return {"review": body.verdict}

    @app.get("/api/resources/media")
    async def browse_media(
        _token: Annotated[str, Depends(require_ready)],
        kind: Literal["emoji", "image_assets"] = "emoji",
        q: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
        status: EmojiStatus = "",
        removed: bool = False,
        image_format: Annotated[EmojiFormat, Query(alias="format")] = "",
        sort: EmojiSort = "recent",
        review: EmojiReview | Literal[""] = "",
    ) -> dict[str, Any]:
        if kind == "emoji":
            entries, total = (
                await asyncio.to_thread(
                    emojis.browse_page,
                    q.strip(),
                    offset,
                    limit,
                    status,
                    removed=removed,
                    image_format=image_format,
                    sort=sort,
                    review=review,
                )
                if emojis is not None
                else ([], 0)
            )
            return {
                "enabled": emojis is not None,
                "total": total,
                "counts": await asyncio.to_thread(emojis.catalog_counts) if emojis else None,
                "capacity": {
                    "active": await asyncio.to_thread(emojis.registration_count),
                    "limit": emojis.registration_limit,
                }
                if emojis
                else None,
                "next_offset": offset + limit if offset + limit < total else None,
                "items": [
                    {
                        **asdict(entry),
                        "text": entry.description,
                        "digest": entry.digest if entry.has_file else None,
                        "entry_key": entry.entry_key,
                        "media_kind": "emoji",
                    }
                    for entry in entries
                ],
            }
        rows, total = (
            await asyncio.to_thread(
                memory.browse_history_page,
                q.strip(),
                "",
                offset,
                limit,
                source_table="image_assets",
            )
            if memory
            else ([], 0)
        )
        return {
            "enabled": memory is not None,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
            "items": [
                {
                    "text": row.text,
                    "source": asdict(row.source),
                    "status": row.status,
                    "digest": next(
                        (
                            match[1]
                            for item in row.evidence
                            if item.kind == "attachment_archived"
                            and (match := _ATTACHMENT.fullmatch(item.reference))
                        ),
                        None,
                    ),
                    "media_kind": next(
                        (item.reference for item in row.evidence if item.kind == "media_type"), ""
                    ),
                }
                for row in rows
            ],
        }

    @app.get("/api/resources/media/{digest}")
    async def read_media(
        digest: str,
        _token: Annotated[str, Depends(require_ready)],
        kind: Literal["emoji", "image_assets"] = "image_assets",
    ) -> Response:
        if kind == "emoji":
            data = await asyncio.to_thread(emojis.read_catalog_media, digest) if emojis else None
            if data is None:
                raise HTTPException(404, "找不到表情原件。")
            return Response(
                data,
                media_type=emoji_media_type(data),
                headers={"X-Content-Type-Options": "nosniff"},
            )
        if memory is not None and (
            bundled := await asyncio.to_thread(memory.read_bundle_image, digest)
        ):
            data, media_type = bundled
            return Response(
                data, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"}
            )
        if memory_dir is None or _ATTACHMENT.fullmatch(f"attachments/{digest}") is None:
            raise HTTPException(404, "找不到归档图片。")
        directory = (memory_dir / "attachments").resolve()
        path = (directory / digest).resolve()
        if path.parent != directory or not directory.is_relative_to(memory_dir.resolve()):
            raise HTTPException(404, "找不到归档图片。")
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise HTTPException(404, "找不到归档图片。") from None
        media_type = emoji_media_type(data)
        if media_type is None:
            raise HTTPException(415, "归档附件无法作为图片预览。")
        return Response(data, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/resources/profiles")
    async def browse_profiles(
        _token: Annotated[str, Depends(require_ready)],
        q: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        rows, total = (
            await asyncio.to_thread(people.browse_profile_resources, q.strip(), offset, limit)
            if people
            else ([], 0)
        )
        return {
            "enabled": people is not None,
            "items": rows,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    aliases_path = "/api/resources/profiles/{profile_id}/aliases"

    @app.get(aliases_path)
    async def get_profile_aliases(
        profile_id: int, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, list[str] | None]:
        if people is None:
            raise HTTPException(409, "人物资料当前关闭。")
        details = await asyncio.to_thread(people.profile_alias_details, profile_id)
        if details is None:
            raise HTTPException(404, "找不到已关联账号的画像来源。")
        return {**asdict(details), "effective_aliases": details.effective_aliases}

    @app.patch(aliases_path)
    async def set_profile_aliases(
        profile_id: int,
        body: ProfileAliasUpdate,
        request: Request,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, list[str] | None]:
        if same_origin is None or not same_origin(request):
            raise HTTPException(403, "请在当前网页上维护人物别名。")
        if people is None:
            raise HTTPException(409, "人物资料当前关闭。")
        try:
            details = await asyncio.to_thread(people.set_profile_aliases, profile_id, body.aliases)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if details is None:
            raise HTTPException(404, "找不到已关联账号的画像来源。")
        return {**asdict(details), "effective_aliases": details.effective_aliases}

    @app.get("/api/resources/pictures")
    async def list_pictures(
        _token: Annotated[str, Depends(require_ready)],
        q: Annotated[str, Query(max_length=200)] = "",
        availability: Literal["", "archived", "reference"] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        rows, total = (
            await asyncio.to_thread(
                browse_pictures,
                database,
                memory is not None,
                images is not None,
                q.strip(),
                availability,
                offset,
                limit,
            )
            if database is not None
            else ([], 0)
        )
        return {
            "enabled": database is not None and (memory is not None or images is not None),
            "items": rows,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    @app.get("/api/resources/images")
    async def browse_images(
        _token: Annotated[str, Depends(require_ready)],
        before: Annotated[int | None, Query(gt=0)] = None,
        similar_to: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        if images is None:
            return {"enabled": False, "items": [], "total": 0, "next_before": None}
        if similar_to is not None:
            matches = await asyncio.to_thread(images.similar, similar_to, limit)
            if matches is None:
                raise HTTPException(404, "这张图片没有索引。")
            return {"enabled": True, "items": matches, "next_before": None}
        rows, total = await asyncio.to_thread(images.browse_page, before, limit + 1)
        return {
            "enabled": True,
            "items": rows[:limit],
            "total": total,
            "next_before": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @app.get("/api/resources/learning")
    async def browse_learning(
        _token: Annotated[str, Depends(require_ready)],
        kind: LanguageKind = LanguageKind.EXPRESSION,
        recall: RecallFilter = "",
        jargon_status: JargonStatus | None = None,
        shared: bool = False,
        removed: bool = False,
        conversation_key: str = "",
        origin_conversation_key: str = "",
        authorized_conversation_key: str = "",
        q: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        def query() -> tuple[list[dict[str, Any]], int]:
            if language is None:
                return [], 0
            if conversation_key or authorized_conversation_key:
                targets = known_conversations()
                if any(
                    key and key not in targets
                    for key in (conversation_key, authorized_conversation_key)
                ):
                    return [], 0
            if origin_conversation_key and origin_conversation_key not in known_conversations(
                include_temporary=True
            ):
                return [], 0
            rows, total = language.browse_page(
                kind,
                offset,
                limit,
                conversation_key=conversation_key,
                origin_conversation_key=origin_conversation_key,
                authorized_conversation_key=authorized_conversation_key,
                text=q,
                is_enabled=(recall == "enabled") if recall else None,
                jargon_status=jargon_status,
                shared=shared,
                is_removed=removed,
            )
            return [
                {**asdict(entry), "cluster_display_version": display_version(entry)}
                if kind == LanguageKind.EXPRESSION
                else asdict(entry)
                for entry in rows
            ], total

        rows, total = await asyncio.to_thread(query)
        return {
            "enabled": language is not None,
            "items": rows,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    @app.get("/api/resources/learning/origins")
    async def list_learning_origins(
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, Any]:
        targets = await asyncio.to_thread(known_conversations, include_temporary=True)
        return {"items": [{"key": key, "label": label} for key, label in targets.items()]}

    @app.get("/api/resources/learning/scope")
    async def list_learning_scope_targets(
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, Any]:
        targets = await asyncio.to_thread(known_conversations)
        return {"items": [{"key": key, "label": label} for key, label in targets.items()]}

    @app.post("/api/resources/learning/scope")
    async def update_learning_scope(
        request: Request,
        body: LearningScopeUpdate,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, Any]:
        if same_origin is None or not same_origin(request):
            raise HTTPException(403, "请从当前网页设置语言包会话范围。")
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        targets = await asyncio.to_thread(known_conversations)
        keys = tuple(dict.fromkeys(body.conversation_keys))
        if any(key not in targets for key in keys):
            raise HTTPException(422, "所选目标不是已知群聊或普通私聊。")
        before = await asyncio.to_thread(
            language.package_scope, body.kind, body.entry_key, keys, confirm=body.confirm
        )
        if before is None:
            raise HTTPException(404, "未找到对应类别的语言包记录。")
        return {
            "before": [targets.get(key, key) for key in before],
            "after": [targets[key] for key in keys],
            "confirmed": body.confirm,
        }

    @app.get("/api/resources/memory")
    async def browse_memory(
        _token: Annotated[str, Depends(require_ready)],
        kind: Literal["all", "history", "live"] = "all",
        category: MemoryCategory = "",
        recall: RecallFilter = "",
        removed: bool = False,
        source_table: Annotated[str, Query(max_length=100)] = "",
        q: Annotated[str, Query(max_length=200)] = "",
        status: Literal[
            "", "active", "inactive", "conflicted", "superseded", "retracted", "deleted", "missing"
        ] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
    ) -> dict[str, Any]:
        if removed and kind != "all":
            raise HTTPException(422, "请在统一文字记忆目录查看已移除项。")
        if kind == "all":
            try:
                rows, total, counts = (
                    await asyncio.to_thread(
                        memory.browse_text_resources,
                        q.strip(),
                        category,
                        status,
                        recall,
                        offset,
                        limit,
                        removed=removed,
                        can_show_summary=can_show_summary,
                    )
                    if memory
                    else ([], 0, {})
                )
            except SummaryVisibilityChangedError:
                raise HTTPException(
                    409, "资料的展示授权已变化，请刷新。", headers={"Cache-Control": "no-store"}
                ) from None
            return {
                "enabled": memory is not None,
                "items": rows,
                "total": total,
                "counts": counts,
                "next_offset": offset + limit if offset + limit < total else None,
            }

        def query() -> tuple[list[dict[str, Any]], int]:
            if memory is None:
                return [], 0
            if kind == "live":
                live_rows, total = memory.browse_live_page(q.strip(), offset, limit, recall=recall)
                return [asdict(row) for row in live_rows], total
            rows, total = memory.browse_history_page(
                q.strip(), status, offset, limit, recall=recall, source_table=source_table
            )
            return [asdict(row) for row in rows], total

        rows, total = await asyncio.to_thread(query)
        counts = (
            await asyncio.to_thread(memory.count_history_sources)
            if memory and kind == "history" and offset == 0
            else {}
        )
        return {
            "enabled": memory is not None,
            "counts": counts,
            "items": rows,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    @app.get("/api/resources/memory/facts/detail")
    async def browse_summary_fact(
        _token: Annotated[str, Depends(require_ready)],
        response: Response,
        source: Annotated[str, Query(min_length=1, max_length=4096)],
    ) -> dict[str, Any]:
        row = None
        if memory is not None and can_show_summary is not None:
            try:
                location = MemorySource.decode(source)
            except (ValueError, TypeError, RecursionError):
                location = None
            if location is not None:
                row = await asyncio.to_thread(memory.summary_resource, location, can_show_summary)
        if row is None:
            raise HTTPException(404, "未找到事实。", headers={"Cache-Control": "no-store"})
        response.headers["Cache-Control"] = "no-store"
        return asdict(row)

    @app.post("/api/resources/learning", status_code=201)
    async def add_learning(
        body: LearningCreate,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, str]:
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        try:
            key = await asyncio.to_thread(language.add_entry, body.kind, body.content.content())
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"entry_key": key}

    @app.patch("/api/resources/learning")
    async def update_learning(
        body: LearningUpdate,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, bool]:
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        if body.content is None and body.is_enabled is None:
            raise HTTPException(422, "请提供修订内容或使用状态。")
        content = body.content.content() if body.content is not None else None
        try:
            found = await asyncio.to_thread(
                language.update_entry, body.kind, body.entry_key, content, body.is_enabled
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if not found:
            raise HTTPException(404, "找不到这条学习记录。")
        return {"ok": True}

    @app.patch("/api/resources/learning/batch")
    async def batch_learning(
        request: Request, body: LearningBatch, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, bool]:
        if same_origin is None or not same_origin(request):
            raise HTTPException(403, "请在当前网页上维护学习记录。")
        if language is None:
            raise HTTPException(409, "学习功能当前关闭。")
        if not await asyncio.to_thread(
            language.set_removed, body.kind, body.entry_keys, body.removed
        ):
            raise HTTPException(404, "未找到全部选中的学习记录，没有修改。")
        return {"ok": True}

    @app.patch("/api/resources/memory/batch")
    async def update_memory_batch(
        request: Request, body: MemoryBatch, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, int]:
        if same_origin is None or not same_origin(request):
            raise HTTPException(403, "请在当前网页上维护文字记忆。")
        if memory is None:
            raise HTTPException(409, "长期记忆功能当前关闭。")
        targets: list[tuple[MemoryKind, int]] = [
            (target.kind, target.entry_id) for target in body.targets
        ]
        try:
            changed = await asyncio.to_thread(memory.set_resources_removed, targets, body.removed)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法维护长期记忆，没有修改。") from None
        if not changed:
            raise HTTPException(409, "所选记录已变更，请刷新列表再试。")
        return {"changed": len(targets)}

    @app.patch("/api/resources/memory")
    async def update_memory(
        body: MemoryUpdate,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, bool]:
        if memory is None:
            raise HTTPException(409, "长期记忆功能当前关闭。")
        if body.text is None and body.is_enabled is None:
            raise HTTPException(422, "请提供修订正文或召回状态。")
        try:
            found = await asyncio.to_thread(
                memory.update_resource, body.kind, body.entry_id, body.text, body.is_enabled
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if not found:
            raise HTTPException(404, "找不到这条长期记忆。")
        return {"ok": True}
