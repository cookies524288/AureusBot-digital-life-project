"""已认证的收件箱历史浏览（D93）。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query

from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.memory.imported_messages import ImportedPlatformHistory
from aureus.timeline.platform_history import LegacyPlatformHistory

from .inbox_history import imported_message, inbox_message, timeline_message, timeline_page

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def register_inbox_routes(
    app: FastAPI,
    store: InboxStore | None,
    require_ready: Callable[..., Awaitable[str]],
    allows_conversation: Callable[[ConversationKey], bool] | None,
    history: LegacyPlatformHistory,
    imported: ImportedPlatformHistory | None,
    archive_account_id: str | None,
) -> None:
    """注册只读查询；收件箱存储每次查询打开独立的数据库连接。"""

    @app.get("/api/inbox/conversations")
    async def list_conversations(
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, Any]:
        enabled = store is not None and allows_conversation is not None
        candidates: dict[ConversationKey, dict[str, Any]] = {}
        if store is not None and allows_conversation is not None:
            rows = await asyncio.to_thread(store.list_conversations, visible_to_user=True)
            candidates = {row.key: asdict(row) for row in rows}
            for route, identifier, timestamp in await asyncio.to_thread(history.routes):
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
        if await asyncio.to_thread(
            history.page, None, before_id=None, limit=1, archive_account_id=archive_account_id
        ):
            archives.append({"archive": "timeline", "name": "旧收件 · 未归类"})
        if imported is not None and await asyncio.to_thread(imported.page, before_id=None, limit=1):
            archives.append({"archive": "imported", "name": "迁入消息 · 未归类"})
        conversations = [
            row for key, row in candidates.items()
            if allows_conversation is not None and allows_conversation(key)
        ]
        catalog: dict[str, Any] = {"enabled": enabled, "conversations": conversations}
        if archives:
            catalog["archives"] = archives
        return catalog

    @app.get("/api/inbox/messages")
    async def list_messages(
        _token: Annotated[str, Depends(require_ready)],
        platform: Annotated[str, Query(min_length=1)],
        account_id: Annotated[str, Query(min_length=1)],
        kind: Annotated[str, Query(min_length=1)],
        recipient_id: Annotated[str, Query(min_length=1)],
        temporary_group_id: Annotated[str | None, Query(min_length=1)] = None,
        before_id: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        before_source: Literal["inbox", "timeline"] = "inbox",
    ) -> dict[str, Any]:
        key = ConversationKey(platform, account_id, kind, recipient_id, temporary_group_id)
        if store is None or allows_conversation is None or not allows_conversation(key):
            return _response([], limit)
        messages: list[dict[str, Any]] = []
        if before_source == "inbox":
            rows = await asyncio.to_thread(
                store.list_messages, key, before_id=before_id, limit=limit + 1, visible_to_user=True
            )
            messages.extend(map(inbox_message, rows))
        remaining = limit + 1 - len(messages)
        if remaining > 0:
            if not allows_conversation(key):
                return _response([], limit)
            old = await asyncio.to_thread(
                timeline_page, store, history, key,
                before_id=before_id if before_source == "timeline" else None, limit=remaining,
            )
            messages.extend(map(timeline_message, old))
        return _response(messages if allows_conversation(key) else [], limit)

    @app.get("/api/inbox/uncategorized")
    async def list_uncategorized(
        _token: Annotated[str, Depends(require_ready)],
        source: Literal["timeline", "imported"],
        before_id: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if source == "timeline":
            rows = await asyncio.to_thread(
                history.page, None, before_id=before_id, limit=limit + 1,
                archive_account_id=archive_account_id,
            )
            messages.extend(map(timeline_message, rows))
        elif imported is not None:
            records = await asyncio.to_thread(imported.page, before_id=before_id, limit=limit + 1)
            messages.extend(map(imported_message, records))
        return _response(messages, limit)


def _response(messages: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    boundary = messages[limit - 1] if len(messages) > limit else None
    response: dict[str, Any] = {
        "messages": messages[:limit], "next_before_id": boundary["id"] if boundary else None
    }
    if boundary and boundary["source"] == "timeline":
        response["next_before_source"] = "timeline"
    return response
