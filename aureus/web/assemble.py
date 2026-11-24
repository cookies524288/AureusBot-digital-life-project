"""把网页服务组装起来：登录状态、密码、设置页、网页通道、服务器（路线图 1.10，D63 第 8 条）。

网页服务和心跳在同一个进程、同一个事件循环里。这里只负责接线，由 aureus chat 在核心组装好之后调用；
核心和别的子系统都不 import 本包（import-linter 检查）。
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager

from aureus.channels import WEB_CHANNEL_NAME, QQAvatars, WebChannel
from aureus.common import Clock
from aureus.core.runtime import Core
from aureus.identity import IdentityEditor

from .auth import LoginLimiter, PasswordStore, WebAuth
from .config_files import ConfigService
from .expression_cluster_grants import ExpressionClusterDisplay
from .runner import WebServer, check_can_start
from .server import create_app
from .session import SessionStore
from .settings import SettingsService


@contextmanager
def open_web(
    core: Core,
    clock: Clock,
    *,
    open_terminal: Callable[[], Awaitable[None]] | None = None,
    request_restart: Callable[[], None] | None = None,
) -> Iterator[WebServer | None]:
    """组装网页服务；退出 with 块时关闭它用到的数据库连接。

    网页通道关着（[web] enabled = false）时什么也不做，返回 None。
    网页通道接到心跳上的名字是 web；没有开始监听，调用方用 asyncio.create_task(server.run()) 启动。

    Args:
        core: 组装好的核心，心跳还没有开始跳也可以。
        clock: 时钟，要和核心用的是同一个。
        open_terminal: 启动器提供的本机终端打开操作。
        request_restart: 启动入口提供的正常重启请求。

    Yields:
        网页服务，或者 None。

    Raises:
        ConfigError: 局域网开放而密码还是默认的 admin（D63 第 6 条）。
    """
    config = core.settings.runtime.web
    if not config.enabled:
        yield None
        return
    passwords = PasswordStore(core.paths.web_password_file)
    # 先检查再打开任何东西：不能启动时不留下半个状态。
    check_can_start(config, password_is_default=passwords.is_default())
    sessions = SessionStore(core.paths.database_file, clock, config.session_days)
    sessions.open()
    try:
        auth = WebAuth(passwords, sessions, LoginLimiter(config, clock))
        channel = WebChannel(core.heartbeat)
        core.heartbeat.attach_channel(WEB_CHANNEL_NAME, channel.show)
        settings = SettingsService(
            editor=IdentityEditor(core.paths.identity_file, core.identity_history),
            history=core.identity_history,
            heartbeat=core.heartbeat,
            auth=auth,
        )
        app = create_app(
            config=config,
            auth=auth,
            sessions=sessions,
            channel=channel,
            timeline=core.timeline,
            settings=settings,
            heartbeat=core.heartbeat,
            clock=clock,
            config_files=ConfigService(core.paths, core.settings.runtime),
            model_runtime=core.models,
            prompt_identity=lambda: core.heartbeat.identity,
            planner_tool_definitions=core.heartbeat.planner_tool_definitions,
            open_terminal=open_terminal,
            request_shutdown=core.heartbeat.request_stop,
            request_restart=request_restart,
            maintenance_paused=lambda: core.heartbeat.maintenance_paused,
            set_maintenance_paused=core.heartbeat.set_maintenance_paused,
            run_model_test=core.gateway.test_model,
            qq_link=core.qq_link,
            inbox_store=core.inbox_store,
            inbox_retention=core.heartbeat.history_retention,
            allows_inbox_conversation=(
                core.qq.allows_inbox_conversation if core.qq is not None else None
            ),
            inbox_archive_account_id=(
                core.settings.runtime.qq.account_id if core.qq is not None else None
            ),
            read_inbox_attachment=core.qq.read_inbox_attachment if core.qq is not None else None,
            qq_desktop=core.qq.desktop if core.qq is not None else None,
            qq_avatars=(
                QQAvatars(core.qq.desktop, clock)
                if core.qq is not None and config.qq_avatars_enabled else None
            ),
            allows_qq_delivery=core.heartbeat.allows_emoji_delivery,
            resource_language=core.language,
            resource_memory=core.memory,
            memory_graph_display=core.memory.graph_display if core.memory is not None else None,
            resource_images=core.inbox_images,
            resource_people=core.people,
            resource_memory_dir=core.paths.memory_dir,
            resource_emojis=core.emoji_store,
            resource_database=core.paths.database_file,
            recall_feedback_enabled=core.settings.runtime.memory.recall_feedback_enabled,
            embedding_progress=core.embedding_progress,
            expression_recall=core.heartbeat.expression_recall,
            expression_cluster_display=(
                ExpressionClusterDisplay(core.paths.database_file, core.language)
                if core.language is not None and core.embedding_progress is not None
                and core.embedding_progress.expression_index is not None
                else None
            ),
        )
        yield WebServer(app, config, home=core.paths.home)
    finally:
        sessions.close()
