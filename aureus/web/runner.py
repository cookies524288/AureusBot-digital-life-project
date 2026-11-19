"""网页服务的启动和停止：和心跳跑在同一个事件循环里（路线图 1.10，D63 第 6、8 条）。

uvicorn 默认会接管 Ctrl+C 和终止信号，还会重设日志配置：这里都关掉。
信号由 aureus chat 处理（先让心跳写完 shut_down 再退出），日志仍走项目自己的设置。
"""

import contextlib
from collections.abc import Generator

import uvicorn
from fastapi import FastAPI

from aureus.common import ConfigError, WebConfig

# 默认只监听本机；allow_remote 打开后监听所有网卡，局域网里的手机才连得上（D53 第 5 条）。
LOOPBACK_HOST = "127.0.0.1"
ALL_INTERFACES_HOST = "0.0.0.0"


class WebServerError(Exception):
    """网页服务没能启动。消息是中文。"""


def listen_host(config: WebConfig) -> str:
    """按配置决定监听哪个地址。"""
    return ALL_INTERFACES_HOST if config.allow_remote else LOOPBACK_HOST


def check_can_start(config: WebConfig, *, password_is_default: bool) -> None:
    """启动前的检查：局域网开放而口令还是默认的 admin 时拒绝启动（D63 第 6 条）。

    Raises:
        ConfigError: 不能这样启动；消息里写明怎么办。
    """
    if config.allow_remote and password_is_default:
        raise ConfigError(
            "aureus.toml 的 [web] allow_remote 打开了，但网页口令还是默认的 admin，"
            "局域网里的人都知道它。请先把 allow_remote 改回 false，在本机打开网页修改口令，"
            "再重新打开 allow_remote。"
        )


class _EmbeddedServer(uvicorn.Server):
    """不接管进程信号的 uvicorn 服务：信号由 aureus chat 统一处理。"""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class WebServer:
    """网页服务。用法：asyncio.create_task(server.run())，要停时调用 request_stop()。"""

    def __init__(self, app: FastAPI, config: WebConfig) -> None:
        self._port = config.port
        self._server = _EmbeddedServer(
            uvicorn.Config(
                app,
                host=listen_host(config),
                port=config.port,
                log_config=None,  # 不让 uvicorn 重设日志
                access_log=False,  # 访问日志会把每次请求的路径都写进日志文件
                lifespan="off",  # 应用没有启动和关闭时要做的事
            )
        )

    @property
    def started(self) -> bool:
        """已经开始监听了吗。"""
        return self._server.started

    async def run(self) -> None:
        """开始服务，直到 request_stop 被调用。

        Raises:
            WebServerError: 端口被占用等原因，没能开始监听。
        """
        try:
            await self._server.serve()
        except SystemExit as error:
            # uvicorn 绑定端口失败时直接退出进程，这里换成一个能被接住的错误。
            raise WebServerError(
                f"网页服务没能在端口 {self._port} 上启动，可能被别的程序占用了；"
                "可以在 aureus.toml 的 [web] 里改 port。"
            ) from error

    def request_stop(self) -> None:
        """请服务停下来；可以重复调用。"""
        self._server.should_exit = True
