"""网页设置页的接口层：请求体校验和三个接口（路线图 1.10，D63 第 1、2、5 条）。

只做翻译：把请求变成 IdentityEdit 交给 SettingsService，把错误换成 HTTP 错误。
接口都是 async def：和心跳在同一个线程里用同一个 SQLite 连接。
口令确认失败返回 403 而不是 401：401 会让页面以为登录过期、跳回登录页。
"""

import math
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aureus.identity import (
    Facts,
    IdentityEdit,
    IdentityEditError,
    IdentityLayer,
    IdentityValidationError,
    PersonaLocks,
    User,
    UserAccount,
)

from .auth import MAX_PASSWORD_LENGTH, LoginLockedError, LoginRejectedError
from .settings import SettingsService

# 单层文字人设最多多少字。没有实测依据：远超正常人设的长度，只为挡住超大请求。
MAX_LAYER_CHARS = 8000
MAX_NAME_CHARS = 100
MAX_ALIASES = 20
MAX_ACCOUNTS = 20


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactsRequest(_Strict):
    """身份事实。"""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    alias_names: list[Annotated[str, Field(max_length=MAX_NAME_CHARS)]] = Field(
        default_factory=list, max_length=MAX_ALIASES
    )
    gender: str = Field(default="", max_length=MAX_NAME_CHARS)
    birthday: str

    @field_validator("birthday")
    @classmethod
    def _check_birthday(cls, value: str) -> str:
        try:
            date.fromisoformat(value.strip())
        except ValueError:
            raise ValueError("出生日期要写成 YYYY-MM-DD") from None
        return value.strip()


class AccountRequest(_Strict):
    """用户在某个平台上的账号。"""

    platform: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    id: str = Field(min_length=1, max_length=MAX_NAME_CHARS)


class UserRequest(_Strict):
    """用户的称呼和账号。"""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    accounts: list[AccountRequest] = Field(default_factory=list, max_length=MAX_ACCOUNTS)


class TextRequest(_Strict):
    """四层文字人设；没写的层保持原样。"""

    immutable: str | None = Field(default=None, max_length=MAX_LAYER_CHARS)
    mutable: str | None = Field(default=None, max_length=MAX_LAYER_CHARS)
    behavior_style: str | None = Field(default=None, max_length=MAX_LAYER_CHARS)
    expression_style: str | None = Field(default=None, max_length=MAX_LAYER_CHARS)


class LocksRequest(_Strict):
    """三层的锁定开关。

    strict=True：只认真正的 true / false，字符串 "yes" 或数字 1 不会被悄悄转成 true；
    三个开关都要写明：漏写的当作 false 的话，只想锁一层会把另外两层的锁一起解开。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    mutable: bool
    behavior_style: bool
    expression_style: bool


class EditRequest(_Strict):
    """一次修改：没写的部分保持原样。改身份事实和不变人格时要带上当前口令。"""

    text: TextRequest = Field(default_factory=TextRequest)
    facts: FactsRequest | None = None
    user: UserRequest | None = None
    locks: LocksRequest | None = None
    password: str | None = Field(default=None, max_length=MAX_PASSWORD_LENGTH)

    def to_edit(self) -> IdentityEdit:
        """换成身份子系统的修改请求。"""
        text = {
            layer: content
            for layer in (
                IdentityLayer.IMMUTABLE,
                IdentityLayer.MUTABLE,
                IdentityLayer.BEHAVIOR_STYLE,
                IdentityLayer.EXPRESSION_STYLE,
            )
            if (content := getattr(self.text, layer.value)) is not None
        }
        facts = None if self.facts is None else Facts(**self.facts.model_dump())
        user = None
        if self.user is not None:
            accounts = [UserAccount(**a.model_dump()) for a in self.user.accounts]
            user = User(name=self.user.name, accounts=accounts)
        locks = None if self.locks is None else PersonaLocks(**self.locks.model_dump())
        return IdentityEdit(text=text, facts=facts, user=user, locks=locks)


class RollbackRequest(_Strict):
    """回退到某一层最近几个版本里的一个。"""

    layer: IdentityLayer
    version_id: int = Field(ge=1)


def _remote_address(request: Request) -> str:
    return request.client.host if request.client else ""


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, LoginLockedError):
        seconds = max(1, math.ceil(error.retry_after.total_seconds()))
        return HTTPException(429, detail=str(error), headers={"Retry-After": str(seconds)})
    if isinstance(error, LoginRejectedError):
        return HTTPException(403, detail="口令不对。")
    if isinstance(error, sqlite3.Error):
        return HTTPException(500, detail="设定已经写进文件，但版本历史没有记上，下次启动时会补记。")
    return HTTPException(422, detail=str(error))


def register_settings_routes(
    app: FastAPI, service: SettingsService, require_ready: Callable[..., Awaitable[str]]
) -> None:
    """把设置页的三个接口挂到应用上；都要求已登录且不在默认口令状态。"""
    errors = (
        LoginLockedError,
        LoginRejectedError,
        IdentityEditError,
        IdentityValidationError,
        sqlite3.Error,
    )

    @app.get("/api/settings")
    async def view(_token: Annotated[str, Depends(require_ready)]) -> dict[str, Any]:
        return service.view()

    @app.put("/api/settings")
    async def edit(
        body: EditRequest, request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, Any]:
        try:
            result = service.apply(
                body.to_edit(), password=body.password, remote_addr=_remote_address(request)
            )
        except errors as error:
            raise _http_error(error) from None
        return {"changed": [layer.value for layer in result.changed_layers]}

    @app.post("/api/settings/rollback")
    async def roll_back(
        body: RollbackRequest,
        request: Request,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, Any]:
        try:
            result = service.roll_back(
                body.layer, body.version_id, remote_addr=_remote_address(request)
            )
        except errors as error:
            raise _http_error(error) from None
        return {"changed": [layer.value for layer in result.changed_layers]}
