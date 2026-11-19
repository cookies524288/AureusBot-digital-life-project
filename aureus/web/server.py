"""网页服务端：登录、改口令、历史对话；WebSocket 和设置页在后面的步骤里补（路线图 1.10，D63）。

只做翻译和登录检查：消息交给 WebChannel，口令和登录状态交给 WebAuth。没登录什么都看不到；
还在用默认口令时，登录后只能进改口令接口（D63 第 6 条）。接口都是 async def：它们要和心跳
在同一个线程里用同一个 SQLite 连接，放进线程池就变成多个线程同时用一个连接。
cookie 设成 HttpOnly 加 SameSite=Strict，别的网站发来的请求带不上它；局域网里是明文 HTTP，
所以不加 Secure（D53 第 5 条）。
"""

import logging
import math
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from aureus.channels import WebChannel
from aureus.common import WebConfig
from aureus.heartbeat import EVENT_MESSAGE
from aureus.timeline import TimelineDatabase, TimelineQuery

from .auth import (
    MAX_PASSWORD_LENGTH,
    LoginLockedError,
    LoginRejectedError,
    PasswordPolicyError,
    WebAuth,
)
from .session import SessionStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "aureus_session"
SECONDS_PER_DAY = 86400
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


class LoginRequest(BaseModel):
    """登录请求。口令长度有上限，超长的请求在算哈希之前就被拒绝。"""

    model_config = ConfigDict(extra="forbid")

    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class ChangePasswordRequest(BaseModel):
    """改口令请求。"""

    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=MAX_PASSWORD_LENGTH)


def _remote_address(request: Request) -> str:
    """来源 IP；拿不到时是空串，is_loopback 会把它当作不是本机（朝安全方向倒）。"""
    return request.client.host if request.client else ""


def _http_error(error: Exception) -> HTTPException:
    """把登录和口令的错误换成 HTTP 错误；消息都是中文，不回显口令。"""
    if isinstance(error, LoginLockedError):
        seconds = max(1, math.ceil(error.retry_after.total_seconds()))
        return HTTPException(429, detail=str(error), headers={"Retry-After": str(seconds)})
    if isinstance(error, LoginRejectedError):
        return HTTPException(401, detail=str(error))
    return HTTPException(422, detail=str(error))


def create_app(
    *,
    config: WebConfig,
    auth: WebAuth,
    sessions: SessionStore,
    channel: WebChannel,
    timeline: TimelineDatabase,
) -> FastAPI:
    """建网页服务的 FastAPI 应用。依赖都由核心组装时传进来，这里不自己打开任何东西。"""
    # 接口文档会把所有接口列给没登录的人看，关掉。
    app = FastAPI(title="AureusBot", docs_url=None, redoc_url=None, openapi_url=None)
    max_age = config.session_days * SECONDS_PER_DAY

    def require_session(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not sessions.touch(token):
            raise HTTPException(401, detail="请先登录。")
        return token

    def require_ready(token: Annotated[str, Depends(require_session)]) -> str:
        if auth.password_is_default:
            raise HTTPException(403, detail="请先修改默认口令。")
        return token

    # ---------- 登录、退出、改口令 ----------

    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, bool]:
        try:
            result = auth.login(body.password, _remote_address(request))
        except (LoginLockedError, LoginRejectedError) as error:
            raise _http_error(error) from None
        response.set_cookie(
            SESSION_COOKIE,
            result.token,
            max_age=max_age,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"must_change_password": result.must_change_password}

    @app.post("/api/logout")
    async def logout(
        token: Annotated[str, Depends(require_session)], response: Response
    ) -> dict[str, bool]:
        sessions.revoke(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/status")
    async def status(_token: Annotated[str, Depends(require_session)]) -> dict[str, bool]:
        return {"must_change_password": auth.password_is_default}

    @app.post("/api/password")
    async def change_password(
        body: ChangePasswordRequest,
        request: Request,
        response: Response,
        _token: Annotated[str, Depends(require_session)],
    ) -> dict[str, bool]:
        try:
            auth.change_password(body.old_password, body.new_password, _remote_address(request))
        except (LoginLockedError, LoginRejectedError, PasswordPolicyError) as error:
            raise _http_error(error) from None
        # 改口令会让所有设备的登录失效，包括这一台：清掉 cookie，让页面回到登录。
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    # ---------- 历史对话 ----------

    @app.get("/api/history")
    async def history(
        _token: Annotated[str, Depends(require_ready)], limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[dict[str, Any]]:
        """最新一批对话，不分通道，每条标明来自哪个通道（D63 第 8 条）。"""
        bounded = min(max(limit, 1), MAX_HISTORY_LIMIT)
        records = timeline.query(
            TimelineQuery(event_type=EVENT_MESSAGE, latest=True, limit=bounded)
        )
        return [
            {
                "id": record.id,
                "time": record.timestamp.isoformat(),
                "who": record.actor_type.value,
                "channel": str(record.metadata.get("channel", "")),
                "content": record.content,
            }
            for record in records
        ]

    # ---------- WebSocket：实时收发（下一步） ----------

    # ---------- 设置页接口（后面的步骤） ----------

    _ = channel  # 下一步的 WebSocket 要用
    return app
