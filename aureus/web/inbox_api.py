"""已认证的收件箱历史浏览（D93）。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from aureus.channels import QQAvatars
from aureus.common import Clock, SystemClock
from aureus.heartbeat.inbox_media import AttachmentReader
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.memory import EmojiStore
from aureus.memory.imported_messages import ImportedPlatformHistory
from aureus.timeline.platform_history import LegacyPlatformHistory, LegacyRoute

from .inbox_history import imported_message, inbox_message, timeline_message, timeline_page

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_PAGE_NUMBER = 2**31 - 1
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
# 旧路由表要扫整条时间线，变化很慢；页面每次打开不必重新聚合（D121）。
ROUTE_CACHE_SECONDS = 60.0
type RouteList = list[tuple[LegacyRoute, int, datetime]]


def _conversation_key(
    platform: Annotated[str, Query(min_length=1)],
    account_id: Annotated[str, Query(min_length=1)],
    kind: Annotated[str, Query(min_length=1)],
    recipient_id: Annotated[str, Query(min_length=1)],
    temporary_group_id: Annotated[str | None, Query(min_length=1)] = None,
) -> ConversationKey:
    """解码网页当前所选会话，不代表该会话已经授权。"""
    return ConversationKey(platform, account_id, kind, recipient_id, temporary_group_id)


def register_inbox_routes(
    app: FastAPI,
    store: InboxStore | None,
    require_ready: Callable[..., Awaitable[str]],
    allows_conversation: Callable[[ConversationKey], bool] | None,
    history: LegacyPlatformHistory,
    imported: ImportedPlatformHistory | None,
    archive_account_id: str | None,
    read_attachment: AttachmentReader | None = None,
    emojis: EmojiStore | None = None,
    avatars: QQAvatars | None = None,
    is_active: Callable[[str], bool] | None = None,
    *,
    is_replying: Callable[[ConversationKey], bool] | None = None,
    clock: Clock | None = None,
) -> None:
    """注册只读查询；收件箱存储每次查询打开独立的数据库连接。"""
    route_clock = clock or SystemClock()
    route_cache: tuple[datetime, RouteList] | None = None

    async def cached_routes() -> RouteList:
        nonlocal route_cache
        now = route_clock.now()
        if route_cache is None or now - route_cache[0] >= timedelta(seconds=ROUTE_CACHE_SECONDS):
            route_cache = (now, await asyncio.to_thread(history.routes))
        return route_cache[1]

    @app.get("/api/inbox/typing")
    async def inbox_typing(
        _token: Annotated[str, Depends(require_ready)],
        key: Annotated[ConversationKey, Depends(_conversation_key)],
    ) -> JSONResponse:
        return JSONResponse(
            {
                "typing": store is not None
                and allows_conversation is not None
                and allows_conversation(key)
                and is_replying is not None
                and is_replying(key)
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/inbox/conversations")
    async def list_conversations(
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, Any]:
        enabled = store is not None and allows_conversation is not None
        candidates: dict[ConversationKey, dict[str, Any]] = {}
        if store is not None and allows_conversation is not None:
            rows = await asyncio.to_thread(store.list_conversations, visible_to_user=True)
            candidates = {row.key: asdict(row) for row in rows}
            for route, identifier, timestamp in await cached_routes():
                key = ConversationKey(*route)
                if key not in candidates:
                    name = f"群 {key.recipient_id}" if key.kind == "group" else key.recipient_id
                    candidates[key] = {
                        "key": asdict(key),
                        "name": name,
                        "unread_count": 0,
                        "last_message_id": identifier,
                        "last_timestamp": timestamp,
                    }
        archives = []
        if await asyncio.to_thread(history.has_archive, archive_account_id):
            archives.append({"archive": "timeline", "name": "旧收件 · 未归类"})
        if imported is not None and await asyncio.to_thread(imported.has_any):
            archives.append({"archive": "imported", "name": "迁入消息 · 未归类"})
        conversations = [
            row
            for key, row in candidates.items()
            if allows_conversation is not None and allows_conversation(key)
        ]
        catalog: dict[str, Any] = {"enabled": enabled, "conversations": conversations}
        if archives:
            catalog["archives"] = archives
        return catalog

    @app.get("/api/inbox/messages")
    async def list_messages(
        _token: Annotated[str, Depends(require_ready)],
        key: Annotated[ConversationKey, Depends(_conversation_key)],
        before_id: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        before_source: Literal["inbox", "timeline"] = "inbox",
        page: Annotated[int | None, Query(ge=1, le=MAX_PAGE_NUMBER)] = None,
    ) -> dict[str, Any]:
        if page is not None and (before_id is not None or before_source != "inbox"):
            raise HTTPException(422, "页码不能与历史游标同时使用。")
        if store is None or allows_conversation is None or not allows_conversation(key):
            return _response([], limit)
        messages: list[dict[str, Any]] = []
        offset = ((page or 1) - 1) * limit
        if before_source == "inbox":
            rows = await asyncio.to_thread(
                store.list_messages,
                key,
                before_id=before_id,
                limit=limit + 1,
                offset=offset,
                visible_to_user=True,
            )
            messages.extend(inbox_message(row, include_attachment_ids=page is None) for row in rows)
        remaining = limit + 1 - len(messages)
        if remaining > 0:
            if not allows_conversation(key):
                return _response([], limit)
            skip = 0
            if offset and not messages:
                size = await asyncio.to_thread(store.count_visible_messages, key)
                skip = max(0, offset - size)
            old = await asyncio.to_thread(
                timeline_page,
                store,
                history,
                key,
                before_id=before_id if before_source == "timeline" else None,
                limit=remaining,
                skip=skip,
            )
            messages.extend(map(timeline_message, old))
        return _response(messages if allows_conversation(key) else [], limit)

    @app.get("/api/inbox/messages/{identifier}/images/{position}")
    async def read_image(
        token: Annotated[str, Depends(require_ready)],
        key: Annotated[ConversationKey, Depends(_conversation_key)],
        identifier: Annotated[int, Path(gt=0, le=2**63 - 1)],
        position: Annotated[int, Path(ge=0)],
        view: Literal["image", "description"] = "image",
    ) -> Response:
        def authorized() -> bool:
            return (
                is_active is not None
                and is_active(token)
                and allows_conversation is not None
                and allows_conversation(key)
            )

        unavailable = HTTPException(404, "这个会话中的图片暂不可读取。")
        if store is None or not authorized():
            raise unavailable
        image = await asyncio.to_thread(store.get_visible_image, key, identifier, position)
        if image is None or not authorized():
            raise unavailable
        perception, attachment, note = image
        if view == "description":
            description = None
            if note is not None and note.source == "vlm":
                description = note.value
            elif note is not None and note.source == "emoji_catalog" and emojis is not None:
                description = await asyncio.to_thread(emojis.read_catalog_description, note.value)
            current = await asyncio.to_thread(store.get_visible_image, key, identifier, position)
            if current != image or not authorized():
                raise unavailable
            return JSONResponse(
                {
                    "description": description,
                    "source": note.source if description and note else None,
                },
                headers={"Cache-Control": "no-store"},
            )
        if read_attachment is None:
            raise unavailable
        media = await read_attachment(perception, attachment)
        if media is None or media[1] not in IMAGE_TYPES or not authorized():
            raise unavailable
        current = await asyncio.to_thread(store.get_visible_image, key, identifier, position)
        if current is None or current[:2] != image[:2] or not authorized():
            raise unavailable
        return Response(
            media[0],
            media_type=media[1],
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
            },
        )

    @app.get("/api/inbox/messages/{identifier}/avatar")
    async def read_avatar(
        request: Request,
        token: Annotated[str, Depends(require_ready)],
        key: Annotated[ConversationKey, Depends(_conversation_key)],
        identifier: Annotated[int, Path(gt=0, le=2**63 - 1)],
    ) -> Response:
        def authorized() -> bool:
            return (
                allows_conversation is not None
                and allows_conversation(key)
                and is_active is not None
                and is_active(token)
            )

        unavailable = HTTPException(404, "这条消息没有可读取的 QQ 头像。")
        if (
            request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")
            or avatars is None
            or store is None
            or not authorized()
        ):
            raise unavailable
        message = await asyncio.to_thread(store.get_visible_message, key, identifier)
        if message is None or (avatar := await avatars.read(message, authorized)) is None:
            raise unavailable
        return Response(
            avatar[0],
            media_type=avatar[1],
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
            },
        )

    @app.get("/api/inbox/uncategorized")
    async def list_uncategorized(
        _token: Annotated[str, Depends(require_ready)],
        source: Literal["timeline", "imported"],
        before_id: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        page: Annotated[int | None, Query(ge=1, le=MAX_PAGE_NUMBER)] = None,
    ) -> dict[str, Any]:
        if page is not None and before_id is not None:
            raise HTTPException(422, "页码不能与历史游标同时使用。")
        messages: list[dict[str, Any]] = []
        offset = ((page or 1) - 1) * limit
        if source == "timeline":
            rows = await asyncio.to_thread(
                history.page,
                None,
                before_id=before_id,
                limit=limit + 1,
                archive_account_id=archive_account_id,
                offset=offset,
            )
            messages.extend(map(timeline_message, rows))
        elif imported is not None:
            records = await asyncio.to_thread(
                imported.page, before_id=before_id, limit=limit + 1, offset=offset
            )
            messages.extend(map(imported_message, records))
        return _response(messages, limit)


def _response(messages: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    boundary = messages[limit - 1] if len(messages) > limit else None
    response: dict[str, Any] = {
        "messages": messages[:limit],
        "next_before_id": boundary["id"] if boundary else None,
    }
    if boundary and boundary["source"] == "timeline":
        response["next_before_source"] = "timeline"
    return response
