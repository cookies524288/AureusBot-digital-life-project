"""网页服务端：登录、改口令、历史对话、WebSocket、设置页和首页（路线图 1.10，D63）。

只做翻译和登录检查：消息交给 WebChannel，口令和登录状态交给 WebAuth。没登录什么都看不到；
还在用默认口令时，登录后只能进改口令接口（D63 第 6 条）。接口都是 async def：它们要和心跳
在同一个线程里用同一个 SQLite 连接，放进线程池就变成多个线程同时用一个连接。
cookie 设成 HttpOnly 加 SameSite=Strict，别的网站发来的请求带不上它；局域网里是明文 HTTP，
所以不加 Secure（D53 第 5 条）。
"""

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from aureus.channels import WebChannel
from aureus.common import Clock, WebConfig
from aureus.heartbeat import EVENT_MESSAGE, OutgoingMessage
from aureus.timeline import TimelineDatabase, TimelineQuery

from .auth import (
    MAX_PASSWORD_LENGTH,
    LoginLockedError,
    LoginRejectedError,
    PasswordPolicyError,
    WebAuth,
)
from .config_api import register_config_routes
from .config_files import ConfigService
from .session import SessionStore
from .settings import SettingsService
from .settings_api import register_settings_routes
from .timeline_api import HeartbeatView, register_timeline_routes

logger = logging.getLogger(__name__)

SESSION_COOKIE = "aureus_session"
SECONDS_PER_DAY = 86400
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200
# WebSocket 的关闭码 1008：违反策略（没登录、来源不对、登录已失效）。
WS_POLICY_VIOLATION = 1008
# 每个连着的网页最多攒多少条待推送的消息；网页收得太慢就丢掉新消息并记日志，不拖住心跳。
OUTBOX_LIMIT = 256
# 连着期间，隔多久核对一次登录是否还有效（退出所有设备、改口令后要把已连上的踢掉）。
SESSION_RECHECK_SECONDS = 30.0
# 网页本身是随代码放在 static 目录里的一个静态文件，登录页、聊天页和设置页都在里面；
# 页面不含任何数据，数据都走要登录的接口。路径从本文件派生，不写死（D38）。
INDEX_PAGE = Path(__file__).parent / "static" / "index.html"


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


def _same_origin(socket: WebSocket) -> bool:
    """握手请求的 Origin 和 Host 是不是同一个地址。

    cookie 的 SameSite 管不到 WebSocket 握手：别的网站的页面可以让用户的浏览器带着 cookie
    连进来，所以要单独核对。浏览器连 WebSocket 时一定会带 Origin；没有就当作不是浏览器，拒绝。
    """
    origin = socket.headers.get("origin")
    host = socket.headers.get("host")
    if not origin or not host:
        return False
    return urlsplit(origin).netloc == host


async def _send_error(socket: WebSocket, detail: str) -> None:
    await socket.send_json({"type": "error", "detail": detail})


async def _push(
    socket: WebSocket,
    outbox: "asyncio.Queue[OutgoingMessage]",
    sessions: SessionStore,
    token: str,
) -> None:
    """把心跳交回的消息推给这个网页；登录失效了就把它踢下线。"""
    try:
        while True:
            try:
                message = await asyncio.wait_for(outbox.get(), timeout=SESSION_RECHECK_SECONDS)
            except TimeoutError:
                message = None
            if not sessions.touch(token):
                await socket.close(code=WS_POLICY_VIOLATION)
                return
            if message is not None:
                await socket.send_json(
                    {
                        "type": message.kind.value,
                        "sender": message.sender_name,
                        "content": message.content,
                        "channel": message.channel,
                    }
                )
    except Exception:
        # 连接已经断了或发不出去：这个网页的推送结束，不影响别的网页和心跳。
        logger.debug("网页连接的推送结束", exc_info=True)


async def _listen(
    socket: WebSocket, channel: WebChannel, sessions: SessionStore, token: str
) -> None:
    """收网页上说的话，交给网页通道；格式不对或太长只回一条错误，不断开连接。"""
    while True:
        raw = await socket.receive_text()
        if not sessions.touch(token):
            await socket.close(code=WS_POLICY_VIOLATION)
            return
        try:
            text = json.loads(raw)["text"]
            if not isinstance(text, str):
                raise TypeError("text 不是字符串")
        except (ValueError, KeyError, TypeError):
            await _send_error(socket, "消息格式不对。")
            continue
        try:
            accepted = channel.send_from_user(text)
        except ValueError as error:
            await _send_error(socket, str(error))
            continue
        if not accepted:
            await _send_error(socket, "正在关闭，这句没有收到。")


def create_app(
    *,
    config: WebConfig,
    auth: WebAuth,
    sessions: SessionStore,
    channel: WebChannel,
    timeline: TimelineDatabase,
    settings: SettingsService | None = None,
    heartbeat: HeartbeatView | None = None,
    clock: Clock | None = None,
    config_files: ConfigService | None = None,
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

    # ---------- WebSocket：实时收发 ----------

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        token = socket.cookies.get(SESSION_COOKIE, "")
        # 来源不对、没登录、还在用默认口令：握手阶段就拒绝，和 HTTP 接口的规则一致。
        if not _same_origin(socket) or not sessions.touch(token) or auth.password_is_default:
            await socket.close(code=WS_POLICY_VIOLATION)
            return
        await socket.accept()
        outbox: asyncio.Queue[OutgoingMessage] = asyncio.Queue(maxsize=OUTBOX_LIMIT)
        unsubscribe = channel.subscribe(outbox.put_nowait)
        pusher = asyncio.create_task(_push(socket, outbox, sessions, token))
        try:
            await _listen(socket, channel, sessions, token)
        except (WebSocketDisconnect, RuntimeError):
            pass  # 网页断开，或者已经被踢下线
        finally:
            unsubscribe()
            pusher.cancel()

    # ---------- 设置页接口 ----------

    if settings is not None:
        register_settings_routes(app, settings, require_ready)

    if heartbeat is not None and clock is not None:
        register_timeline_routes(
            app,
            timeline=timeline,
            heartbeat=heartbeat,
            clock=clock,
            visibility=channel.user_visibility,
            require_ready=require_ready,
            restart_required=lambda: config_files.restart_required if config_files else False,
        )

    if config_files is not None:
        register_config_routes(app, config_files, auth, require_ready)

    # ---------- 网页本身 ----------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """登录前也要能打开页面，所以不检查登录；页面里没有数据，数据都要登录后从接口取。"""
        return FileResponse(INDEX_PAGE, headers={"Cache-Control": "no-store"})

    return app
