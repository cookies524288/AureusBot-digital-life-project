"""网页服务端：登录、改密码、历史对话、WebSocket、设置页和首页（路线图 1.10，D63）。

只做翻译和登录检查：消息交给 WebChannel，密码和登录状态交给 WebAuth。没登录什么都看不到；
还在用默认密码时，登录后只能进改密码接口（D63 第 6 条）。接口都是 async def：它们要和心跳
在同一个线程里用同一个 SQLite 连接，放进线程池就变成多个线程同时用一个连接。
cookie 设成 HttpOnly 加 SameSite=Strict，别的网站发来的请求带不上它；局域网里是明文 HTTP，
所以不加 Secure（D53 第 5 条）。
"""

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from aureus.channels import WebChannel
from aureus.common import Clock, WebConfig
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import EVENT_MESSAGE, OutgoingMessage
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_store import InboxStore
from aureus.identity import Identity
from aureus.memory import EmojiStore, ExpressionClusterStore, LanguageStore, MemoryIndex
from aureus.organs import OrganResponse
from aureus.people import LINK_COMMAND, LINK_LIFETIME, AccountLink, PeopleDirectory, PeopleError
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

from .auth import (
    MAX_PASSWORD_LENGTH,
    LoginLockedError,
    LoginRejectedError,
    PasswordPolicyError,
    WebAuth,
    is_loopback,
)
from .config_api import register_config_routes
from .config_files import ConfigService
from .expression_cluster_api import ExpressionClusterGrants, register_expression_cluster_routes
from .inbox_api import register_inbox_routes
from .language_package_api import register_language_package_routes
from .memory_graph_api import GraphGrants, register_memory_graph_routes
from .memory_inspection_api import register_memory_inspection_routes
from .memory_transfer_api import register_memory_transfer_routes
from .prompt_api import register_prompt_routes
from .recall_feedback_api import register_recall_feedback_routes
from .resource_api import register_resource_routes
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
# 连着期间，隔多久核对一次登录是否还有效（退出所有设备、改密码后要把已连上的踢掉）。
SESSION_RECHECK_SECONDS = 30.0
# 网页本身是随代码放在 static 目录里的一个静态文件，登录页、聊天页和设置页都在里面；
# 页面不含任何数据，数据都走要登录的接口。路径从本文件派生，不写死（D38）。
INDEX_PAGE = Path(__file__).parent / "static" / "index.html"


class QQLinkRequest(BaseModel):
    """网页发起绑定时提供的 QQ 账号"""

    account_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")


class LoginRequest(BaseModel):
    """登录请求。密码长度有上限，超长的请求在算哈希之前就被拒绝。"""

    model_config = ConfigDict(extra="forbid")

    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class ChangePasswordRequest(BaseModel):
    """改密码请求。"""

    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=MAX_PASSWORD_LENGTH)


def _remote_address(request: Request) -> str:
    """来源 IP；拿不到时是空串，is_loopback 会把它当作不是本机（朝安全方向倒）。"""
    return request.client.host if request.client else ""


def _local_terminal_request(request: Request) -> bool:
    host = request.url.hostname or ""
    return is_loopback(_remote_address(request)) and (host == "localhost" or is_loopback(host))


def _same_http_origin(request: Request) -> bool:
    return request.headers.get("origin") == f"{request.url.scheme}://{request.url.netloc}"


def _http_error(error: Exception) -> HTTPException:
    """把登录和密码的错误换成 HTTP 错误；消息都是中文，不回显密码。"""
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
                        **({"id": message.timeline_id} if message.timeline_id is not None else {}),
                        **({"time": message.timestamp.isoformat()} if message.timestamp else {}),
                    }
                )
                if message.confirm_delivery is not None:
                    message.confirm_delivery()
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
    model_runtime: ModelRuntime | None = None,
    prompt_identity: Callable[[], Identity] | None = None,
    open_terminal: Callable[[], Awaitable[None]] | None = None,
    request_shutdown: Callable[[], None] | None = None,
    request_restart: Callable[[], None] | None = None,
    maintenance_paused: Callable[[], bool] | None = None,
    set_maintenance_paused: Callable[[bool], None] | None = None,
    run_model_test: Callable[[Callable[[], Awaitable[OrganResponse]]], Awaitable[OrganResponse]]
    | None = None,
    qq_link: AccountLink | None = None,
    inbox_store: InboxStore | None = None,
    resource_language: LanguageStore | None = None,
    resource_memory: MemoryIndex | None = None,
    resource_images: InboxImageIndex | None = None,
    resource_people: PeopleDirectory | None = None,
    resource_memory_dir: Path | None = None,
    resource_emojis: EmojiStore | None = None,
    resource_database: Path | None = None,
    recall_feedback_enabled: bool = False,
    memory_graph_grants: GraphGrants | None = None,
    expression_cluster_store: ExpressionClusterStore | None = None,
    expression_cluster_grants: ExpressionClusterGrants | None = None,
) -> FastAPI:
    """建网页服务的 FastAPI 应用。依赖都由核心组装时传进来，这里不自己打开任何东西。"""
    # 接口文档会把所有接口列给没登录的人看，关掉。
    app = FastAPI(title="AureusBot", docs_url=None, redoc_url=None, openapi_url=None)
    max_age = config.session_days * SECONDS_PER_DAY
    instance = uuid4().hex

    # 同步依赖会被 FastAPI 放进工作线程，不能和事件循环并发使用同一个 SQLite 连接。
    async def require_session(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not sessions.touch(token):
            raise HTTPException(401, detail="请先登录。")
        return token

    async def require_ready(token: Annotated[str, Depends(require_session)]) -> str:
        if auth.password_is_default:
            raise HTTPException(403, detail="请先修改默认密码。")
        return token

    async def require_inspection_access(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not sessions.is_active(token):
            raise HTTPException(401, detail="请先登录。")
        if auth.password_is_default:
            raise HTTPException(403, detail="请先修改默认密码。")
        return token

    # ---------- 登录、退出、改密码 ----------

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
        if qq_link is not None:
            qq_link.revoke(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    if request_shutdown is not None:

        @app.post("/api/shutdown")
        async def shutdown(
            request: Request, _token: Annotated[str, Depends(require_ready)]
        ) -> dict[str, bool]:
            if not _same_http_origin(request):
                raise HTTPException(403, detail="请在当前网页上确认退出。")
            request_shutdown()
            return {"ok": True}

    if qq_link is not None:
        links = qq_link

        async def require_link_session(
            request: Request, token: Annotated[str, Depends(require_ready)]
        ) -> str:
            if not _same_http_origin(request):
                raise HTTPException(403, detail="请在当前网页上操作 QQ 绑定。")
            return token

        @app.post("/api/qq-link")
        async def issue_link(
            body: QQLinkRequest,
            token: Annotated[str, Depends(require_link_session)],
            response: Response,
        ) -> dict[str, str | int]:
            try:
                code = links.issue(token, body.account_id)
            except PeopleError as error:
                raise HTTPException(409, detail=str(error)) from None
            response.headers["Cache-Control"] = "no-store"
            return {
                "command": f"{LINK_COMMAND} {code}",
                "expires_in": int(LINK_LIFETIME.total_seconds()),
            }

        @app.post("/api/qq-link/confirm")
        async def confirm_link(
            token: Annotated[str, Depends(require_link_session)],
        ) -> dict[str, str]:
            try:
                account_id = links.confirm(token)
            except PeopleError as error:
                raise HTTPException(409, detail=str(error)) from None
            timeline.append(
                "account_linked",
                "已通过网页会话和 QQ 私聊验证关联账号。",
                ActorType.USER,
                visibility=VisibilityScope.USER_ONLY,
                metadata={"platform": "qq", "account_id": account_id},
            )
            return {"account_id": account_id}

        @app.delete("/api/qq-link")
        async def revoke_link(
            token: Annotated[str, Depends(require_link_session)],
        ) -> dict[str, bool]:
            links.revoke(token)
            return {"ok": True}

    if request_restart is not None:

        @app.post("/api/restart")
        async def restart(
            request: Request, _token: Annotated[str, Depends(require_ready)]
        ) -> dict[str, str]:
            if not _same_http_origin(request):
                raise HTTPException(403, detail="请在当前网页上确认重启。")
            request_restart()
            return {"instance": instance}

    @app.get("/api/status")
    async def status(
        request: Request, _token: Annotated[str, Depends(require_session)]
    ) -> dict[str, bool | str]:
        result: dict[str, bool | str] = {"must_change_password": auth.password_is_default}
        if qq_link is not None and not auth.password_is_default:
            result["qq_link"] = True
        if request_restart is not None and not auth.password_is_default:
            result["restart_instance"] = instance
        if open_terminal is not None and not auth.password_is_default:
            result["can_open_terminal"] = _local_terminal_request(request)
        return result

    if open_terminal is not None:

        @app.post("/api/terminal/open")
        async def show_terminal(
            request: Request, _token: Annotated[str, Depends(require_ready)]
        ) -> dict[str, bool]:
            if not _local_terminal_request(request) or not _same_http_origin(request):
                raise HTTPException(403, detail="请从本机网页打开 CLI 窗口。")
            try:
                await open_terminal()
            except OSError:
                logger.exception("启动 CLI 窗口失败")
                raise HTTPException(500, detail="无法打开 CLI 窗口，请查看日志。") from None
            return {"ok": True}

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
        # 改密码会让所有设备的登录失效，包括这一台：清掉 cookie，让页面回到登录。
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    # ---------- 历史对话 ----------

    @app.get("/api/history")
    async def history(
        _token: Annotated[str, Depends(require_ready)], limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[dict[str, Any]]:
        """最新一批与用户本人的私聊，每条标明来自哪个通道（D84）"""
        bounded = min(max(limit, 1), MAX_HISTORY_LIMIT)
        records = timeline.query(
            TimelineQuery(
                event_type=EVENT_MESSAGE,
                latest=True,
                limit=bounded,
                visibility=VisibilityScope.USER_ONLY,
            )
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
        # 来源不对、没登录、还在用默认密码：握手阶段就拒绝，和 HTTP 接口的规则一致。
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

    register_inbox_routes(app, inbox_store, require_ready)
    register_language_package_routes(
        app, require_ready, _same_http_origin, resource_language, clock
    )
    register_memory_transfer_routes(
        app,
        require_ready,
        _same_http_origin,
        resource_memory,
        clock,
        require_preview_access=require_inspection_access,
        people=resource_people,
    )
    register_memory_graph_routes(
        app, require_ready, resource_memory, memory_graph_grants, sessions.is_active
    )
    register_expression_cluster_routes(
        app,
        require_ready,
        resource_language,
        expression_cluster_store,
        expression_cluster_grants,
        sessions.is_active,
    )
    register_memory_inspection_routes(
        app, require_inspection_access, resource_memory, channel.user_visibility
    )
    register_resource_routes(
        app,
        require_ready,
        resource_language,
        resource_memory,
        resource_images,
        resource_people,
        resource_memory_dir,
        resource_emojis,
        same_origin=_same_http_origin,
        database=resource_database,
        clock=clock,
        inbox=inbox_store,
    )

    if maintenance_paused is not None and set_maintenance_paused is not None:

        @app.post("/api/power/{action}")
        async def power(
            action: str, request: Request, _token: Annotated[str, Depends(require_ready)]
        ) -> dict[str, bool]:
            if not _same_http_origin(request):
                raise HTTPException(403, detail="请在当前网页上操作开关机。")
            if action not in ("start", "stop"):
                raise HTTPException(404, detail="未知的开关机操作。")
            set_maintenance_paused(action == "stop")
            return {"maintenance_paused": maintenance_paused()}

    if heartbeat is not None and clock is not None:
        register_timeline_routes(
            app,
            timeline=timeline,
            heartbeat=heartbeat,
            clock=clock,
            visibility=channel.user_visibility,
            require_ready=require_ready,
            restart_required=lambda: config_files.restart_required if config_files else False,
            maintenance_paused=maintenance_paused,
            recall_feedback_enabled=recall_feedback_enabled,
        )
        register_recall_feedback_routes(
            app, timeline, require_ready, _same_http_origin, enabled=recall_feedback_enabled
        )

    if config_files is not None:
        register_config_routes(
            app,
            config_files,
            auth,
            require_ready,
            model_runtime,
            run_model_test,
            require_active=require_inspection_access,
        )
        if (
            model_runtime is not None
            and model_runtime.prompts is not None
            and prompt_identity is not None
            and clock is not None
        ):
            register_prompt_routes(
                app,
                store=model_runtime.prompts,
                files=config_files,
                runtime=config_files.runtime,
                identity=prompt_identity,
                clock=clock,
                require_ready=require_ready,
            )

    # ---------- 网页本身 ----------
    # 和 API 一起加载，避免更新磁盘文件后旧进程提供依赖新接口的页面。
    index_html = INDEX_PAGE.read_bytes()
    organ_graph = INDEX_PAGE.with_name("organ-graph.js").read_bytes()
    resources_ui = INDEX_PAGE.with_name("resource-management.js").read_bytes()
    memory_flow_ui = INDEX_PAGE.with_name("memory-flow-transfer.js").read_bytes()
    organ_tile = INDEX_PAGE.with_name("organ-tile.png").read_bytes()

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        """登录前也要能打开页面，所以不检查登录；页面里没有数据，数据都要登录后从接口取。"""
        return HTMLResponse(index_html, headers={"Cache-Control": "no-store"})

    @app.get("/assets/organ-graph.js", include_in_schema=False)
    async def organ_graph_asset() -> Response:
        return Response(
            organ_graph, media_type="text/javascript", headers={"Cache-Control": "no-store"}
        )

    @app.get("/assets/resource-management.js", include_in_schema=False)
    async def resource_management_asset() -> Response:
        return Response(
            resources_ui, media_type="text/javascript", headers={"Cache-Control": "no-store"}
        )

    @app.get("/assets/organ-tile.png", include_in_schema=False)
    async def organ_tile_asset() -> Response:
        return Response(organ_tile, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/assets/memory-flow-transfer.js", include_in_schema=False)
    async def memory_flow_asset() -> Response:
        return Response(
            memory_flow_ui, media_type="text/javascript", headers={"Cache-Control": "no-store"}
        )

    @app.get("/assets/ark-pixel.woff2", include_in_schema=False)
    async def pixel_font() -> FileResponse:
        return FileResponse(INDEX_PAGE.with_name("ark-pixel.woff2"), media_type="font/woff2")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(INDEX_PAGE.with_name("aureus.ico"), media_type="image/x-icon")

    return app
