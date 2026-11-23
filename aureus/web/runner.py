"""网页服务的启动和停止：和心跳跑在同一个事件循环里（路线图 1.10，D63 第 6、8 条）。

uvicorn 默认会接管 Ctrl+C 和终止信号，还会重设日志配置：这里都关掉。
信号由 aureus chat 处理（先让心跳写完 shut_down 再退出），日志仍走项目自己的设置。
"""

import contextlib
import hashlib
import os
import socket
import sys
from collections.abc import Callable, Generator
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from aureus.common import ConfigError, WebConfig

# 默认只监听本机；allow_remote 打开后监听所有网卡，局域网里的手机才连得上（D53 第 5 条）。
LOOPBACK_HOST = "127.0.0.1"
ALL_INTERFACES_HOST = "0.0.0.0"
_PORT_BYTES = 2
_PID_BYTES = 4


def _address_name(home: Path) -> str:
    canonical_home = os.path.normcase(str(home.resolve()))
    return "AureusWeb_" + hashlib.sha256(os.fsencode(canonical_home)).hexdigest()


def running_web_url(home: Path) -> str | None:
    """读取同一数据根目录正在监听的 Windows 网页地址。"""
    instance = running_web_instance(home)
    return instance[0] if instance is not None else None


def running_web_instance(home: Path) -> tuple[str, int] | None:
    """读取网页地址和稳定拥有该服务的启动进程号。"""
    if sys.platform != "win32":
        return None
    try:
        address = SharedMemory(name=_address_name(home))
    except FileNotFoundError:
        return None
    with contextlib.closing(address):
        buffer = address.buf
        assert buffer is not None
        port = int.from_bytes(buffer[:_PORT_BYTES], "big")
        owner = int.from_bytes(buffer[_PORT_BYTES : _PORT_BYTES + _PID_BYTES], "big")
    return (f"http://{LOOPBACK_HOST}:{port}", owner) if port else None


class WebServerError(Exception):
    """网页服务没能启动。消息是中文。"""


def listen_host(config: WebConfig) -> str:
    """按配置决定监听哪个地址。"""
    return ALL_INTERFACES_HOST if config.allow_remote else LOOPBACK_HOST


def check_can_start(config: WebConfig, *, password_is_default: bool) -> None:
    """启动前的检查：局域网开放而密码还是默认的 admin 时拒绝启动（D63 第 6 条）。

    Raises:
        ConfigError: 不能这样启动；消息里写明怎么办。
    """
    if config.allow_remote and password_is_default:
        raise ConfigError(
            "aureus.toml 的 [web] allow_remote 打开了，但网页密码还是默认的 admin，"
            "局域网里的人都知道它。请先把 allow_remote 改回 false，在本机打开网页修改密码，"
            "再重新打开 allow_remote。"
        )


class _EmbeddedServer(uvicorn.Server):
    """不接管进程信号的 uvicorn 服务：信号由 aureus chat 统一处理。"""

    on_started: Callable[[], None] | None = None

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """开始监听后再通知调用方，避免把占用端口的服务当成本次启动的网页。"""
        await super().startup(sockets=sockets)
        if self.started and self.on_started is not None:
            self.on_started()


class WebServer:
    """网页服务。用法：asyncio.create_task(server.run())，要停时调用 request_stop()。"""

    def __init__(self, app: FastAPI, config: WebConfig, *, home: Path | None = None) -> None:
        self._port = config.port
        self._address_name = (
            _address_name(home) if home is not None and sys.platform == "win32" else None
        )
        self._address: SharedMemory | None = None
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

    async def run(
        self, *, on_started: Callable[[], None] | None = None, owner_pid: int | None = None
    ) -> None:
        """开始服务，直到 request_stop 被调用。

        Args:
            on_started: 成功监听后调用的可选函数。
            owner_pid: Windows 托盘外层进程，可跨内核重启保持不变。

        Raises:
            WebServerError: 端口被占用等原因，没能开始监听。
        """

        def publish_address() -> None:
            if self._address_name is not None:
                # Windows 在最后一个句柄关闭时移除映射，异常退出也不会留下旧端口。
                self._address = SharedMemory(
                    name=self._address_name, create=True, size=_PORT_BYTES + _PID_BYTES
                )
                buffer = self._address.buf
                assert buffer is not None
                buffer[_PORT_BYTES : _PORT_BYTES + _PID_BYTES] = (
                    owner_pid or os.getpid()
                ).to_bytes(_PID_BYTES, "big")
                buffer[:_PORT_BYTES] = self._port.to_bytes(_PORT_BYTES, "big")
            if on_started is not None:
                on_started()

        self._server.on_started = publish_address
        try:
            await self._server.serve()
        except SystemExit as error:
            # uvicorn 绑定端口失败时直接退出进程，这里换成一个能被接住的错误。
            raise WebServerError(
                f"网页服务没能在端口 {self._port} 上启动，可能被别的程序占用或被系统保留；"
                "可以在 aureus.toml 的 [web] 里改 port。"
            ) from error
        finally:
            self._clear_address()

    def _clear_address(self) -> None:
        if self._address is not None:
            self._address.close()
            self._address = None

    def request_stop(self) -> None:
        """请服务停下来；可以重复调用。"""
        self._clear_address()
        self._server.should_exit = True
