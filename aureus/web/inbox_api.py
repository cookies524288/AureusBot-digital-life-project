"""已认证的收件箱历史浏览（D93）。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query

from aureus.heartbeat.inbox_store import ConversationKey, InboxStore

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def register_inbox_routes(
    app: FastAPI, store: InboxStore | None, require_ready: Callable[..., Awaitable[str]]
) -> None:
    """注册只读查询；收件箱存储每次查询打开独立的数据库连接。"""

    @app.get("/api/inbox/conversations")
    async def list_conversations(
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, Any]:
        conversations = [] if store is None else await asyncio.to_thread(store.list_conversations)
        return {"enabled": store is not None, "conversations": [asdict(c) for c in conversations]}

    @app.get("/api/inbox/messages")
    async def list_messages(
        _token: Annotated[str, Depends(require_ready)],
        platform: Annotated[str, Query(min_length=1)],
        account_id: Annotated[str, Query(min_length=1)],
        kind: Annotated[str, Query(min_length=1)],
        recipient_id: Annotated[str, Query(min_length=1)],
        temporary_group_id: str | None = None,
        before_id: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        key = ConversationKey(platform, account_id, kind, recipient_id, temporary_group_id)
        messages = []
        if store is not None:
            messages = await asyncio.to_thread(
                store.list_messages, key, before_id=before_id, limit=limit + 1
            )
        return {
            "messages": [
                {
                    "id": message.id,
                    "sender_id": message.perception.sender_id,
                    "sender_name": message.sender_name,
                    "timestamp": message.timestamp,
                    "content": message.perception.content,
                }
                for message in messages[:limit]
            ],
            "next_before_id": messages[limit - 1].id if len(messages) > limit else None,
        }
