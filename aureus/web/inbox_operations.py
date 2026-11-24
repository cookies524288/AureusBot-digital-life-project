"""网页 QQ 操作：核对登录、确认、原件和外发权限，再交给通道。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from aureus.channels.qq_desktop import DesktopError, QQDesktop
from aureus.common import Clock
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore

from .inbox_api import _conversation_key

MAX_TEXT_LENGTH = 2000
MIN_OPERATION_INTERVAL = timedelta(seconds=1)
EntryId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class TextOperation(BaseModel):
    """网页只提交文字及本地可见原件序号。"""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT_LENGTH)]
    quote: EntryId | None = None
    mention: EntryId | None = None


class PokeOperation(BaseModel):
    """选择当前群聊发言者；普通私聊不需要另填对象。"""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    member: EntryId | None = None


def register_inbox_operations(
    app: FastAPI, require_ready: Callable[..., Awaitable[str]],
    is_active: Callable[[str], bool], same_origin: Callable[[Request], bool],
    store: InboxStore | None, desktop: QQDesktop | None, clock: Clock | None,
    allows_delivery: Callable[[], bool] | None,
) -> None:
    """注册有限桌面操作，沿用心跳实际投递前的急停与维护暂停检查。"""
    in_flight = False
    last_operation: datetime | None = None

    def authorize(token: str) -> None:
        if not is_active(token):
            raise HTTPException(401, "登录已失效，没有发送。")
        if allows_delivery is None or not allows_delivery():
            raise HTTPException(403, "急停、维护暂停或停止中，当前不允许操作 QQ。")

    async def known_conversation(key: ConversationKey) -> QQDesktop:
        if desktop is None or store is None or clock is None:
            raise HTTPException(503, "当前运行未接入 QQ 桌面操作。")
        desktop.ready(key)
        if not await asyncio.to_thread(store.list_messages, key, limit=1, visible_to_user=True):
            raise HTTPException(404, "当前没有这段会话的可见 QQ 原件。")
        desktop.ready(key)
        return desktop

    async def read_entry(key: ConversationKey, identifier: int | None) -> InboxMessage | None:
        if identifier is None:
            return None
        assert store is not None
        result = await asyncio.to_thread(store.get_visible_message, key, identifier)
        if result is None:
            raise HTTPException(404, "所选消息不在当前会话的可见原件中。")
        return result

    async def execute[T](
        request: Request, token: str, key: ConversationKey,
        operation: Callable[[QQDesktop, Callable[[], None]], Awaitable[T]],
    ) -> T:
        nonlocal in_flight, last_operation
        if not same_origin(request):
            raise HTTPException(403, "只能在当前登录的 Aureus 页面操作 QQ。")
        authorize(token)
        if in_flight:
            raise HTTPException(429, "上一项 QQ 操作尚未完成。")
        if (
            clock is not None and last_operation is not None
            and clock.now() - last_operation < MIN_OPERATION_INTERVAL
        ):
            raise HTTPException(429, "QQ 操作过于频繁。", headers={"Retry-After": "1"})
        in_flight = True
        try:
            connection = await known_conversation(key)
            assert clock is not None
            last_operation = clock.now()
            result = await operation(connection, lambda: authorize(token))
            if not is_active(token):
                raise HTTPException(401, "登录已失效；该操作已经发出，请先检查 QQ。")
            connection.check_scope(key)
            return result
        except DesktopError as error:
            raise HTTPException(error.status, error.detail) from None
        finally:
            in_flight = False

    @app.get("/api/inbox/operations")
    async def operation_status(
        token: Annotated[str, Depends(require_ready)],
        key: Annotated[ConversationKey, Depends(_conversation_key)],
    ) -> dict[str, bool | str]:
        try:
            authorize(token)
            await known_conversation(key)
            authorize(token)
        except (HTTPException, DesktopError) as error:
            return {"enabled": False, "reason": error.detail}
        return {"enabled": True}

    @app.post("/api/inbox/operations/text")
    async def send_text(
        request: Request, body: TextOperation,
        token: Annotated[str, Depends(require_ready)],
        key: Annotated[ConversationKey, Depends(_conversation_key)],
    ) -> dict[str, str]:
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "请填写要发送的文字。")

        async def operation(connection: QQDesktop, check: Callable[[], None]) -> str:
            quote = await read_entry(key, body.quote)
            mention = await read_entry(key, body.mention)
            return await connection.send_text(key, text, quote, mention, check)

        return {"message_id": await execute(request, token, key, operation)}

    @app.post("/api/inbox/operations/poke")
    async def send_poke(
        request: Request, body: PokeOperation,
        token: Annotated[str, Depends(require_ready)],
        key: Annotated[ConversationKey, Depends(_conversation_key)],
    ) -> dict[str, bool]:
        async def operation(connection: QQDesktop, check: Callable[[], None]) -> None:
            member = await read_entry(key, body.member)
            await connection.send_poke(key, member, check)

        await execute(request, token, key, operation)
        return {"accepted": True}
