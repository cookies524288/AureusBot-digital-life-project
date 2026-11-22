"""把网页服务组装起来：登录状态、密码、设置页、网页通道、服务器（路线图 1.10，D63 第 8 条）。

网页服务和心跳在同一个进程、同一个事件循环里。这里只负责接线，由 aureus chat 在核心组装好之后调用；
核心和别的子系统都不 import 本包（import-linter 检查）。
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager

from aureus.channels import WEB_CHANNEL_NAME, WebChannel
from aureus.common import Clock
from aureus.core.runtime import Core
from aureus.identity import IdentityEditor

from .auth import LoginLimiter, PasswordStore, WebAuth
from .config_files import ConfigService
from .runner import WebServer, check_can_start
from .server import create_app
from .session import SessionStore
from .settings import SettingsService


@contextmanager
def open_web(
    core: Core, clock: Clock, *, open_terminal: Callable[[], Awaitable[None]] | None = None
) -> Iterator[WebServer | None]:
    """组装网页服务；退出 with 块时关闭它用到的数据库连接。

    网页通道关着（[web] enabled = false）时什么也不做，返回 None。
    网页通道接到心跳上的名字是 web；没有开始监听，调用方用 asyncio.create_task(server.run()) 启动。

    Args:
        core: 组装好的核心，心跳还没有开始跳也可以。
        clock: 时钟，要和核心用的是同一个。
        open_terminal: 启动器提供的本机终端打开操作。

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
            config_files=ConfigService(core.paths),
            model_runtime=core.models,
            open_terminal=open_terminal,
        )
        yield WebServer(app, config)
    finally:
        sessions.close()
