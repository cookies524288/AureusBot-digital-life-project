"""公共地基 common：日志。

用成熟的 loguru（MIT 协议），同时输出到终端和数据根目录下的日志文件，
文件按大小滚动、保留一段时间。

密钥不能出现在日志里：消息和异常追踪里出现的已知密钥值，写出之前统一替换成占位符。
注意这只挡「启动时已知的密钥原文」：被改写过的（URL 编码、截断、拆开拼接）挡不住，
所以写日志的代码仍然不应该主动输出密钥，这层过滤是兜底，不是许可（审查 M1）。
日志行的时间戳由 loguru 自己取（真实时间），不走时钟对象：日志用来对照真实世界，
时间线里的时间才走时钟（见 D26）。

项目里有的模块用标准库的 logging（心跳、网关、时间线、终端），有的用 loguru。
为了让两边的日志都进日志文件、都过密钥过滤，设置日志时会把标准库的日志转给 loguru
（做法参考 loguru 文档里的 InterceptHandler 配方，MIT 协议）。
"""

import inspect
import logging
import sys
import traceback
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from aureus.common.config import Settings

if TYPE_CHECKING:
    from loguru import Record

# 密钥被替换成的占位符。
REDACTED = "[已隐藏]"

LOG_FILE_NAME = "aureus.log"

# 单个日志文件超过这个大小就换一个新文件；旧文件保留这么久，之后自动删除。
LOG_ROTATION = "10 MB"
LOG_RETENTION = "14 days"

CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
)
FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS ZZ} | {level: <8} | {name}:{function}:{line} | {message}"
)


class _InterceptHandler(logging.Handler):
    """把标准库 logging 的日志转给 loguru，由 loguru 统一过滤密钥、写文件。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # 往上找到真正打日志的那一行，日志里的“文件:函数:行号”才是对的，不是这个转接器：
        # 先跳过 emit 自己这一帧，再跳过 logging 模块内部的帧。
        frame, depth = inspect.currentframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


# 当前挂在根日志器上的转接处理器，以及挂上之前根日志器的级别（撤销时要还原）。
_bridge: _InterceptHandler | None = None
_root_level_before: int = logging.WARNING


def _remove_stdlib_bridge() -> None:
    """撤掉转接，并把根日志器的级别还原。没有挂过就什么也不做。"""
    global _bridge
    if _bridge is None:
        return
    root = logging.getLogger()
    root.removeHandler(_bridge)
    root.setLevel(_root_level_before)
    _bridge = None


def _install_stdlib_bridge(level: str) -> None:
    """把标准库的日志转给 loguru。可以重复调用：先撤掉旧的。

    只往根日志器上加一个处理器，不动别的处理器：测试里 pytest 的 caplog 照常能用。
    根日志器的级别设成同一个级别，低级别的日志（比如第三方库的 DEBUG）不会白白走一遍转接。
    """
    global _bridge, _root_level_before
    _remove_stdlib_bridge()
    root = logging.getLogger()
    _root_level_before = root.level
    _bridge = _InterceptHandler()
    root.addHandler(_bridge)
    root.setLevel(level)


def _build_redactor(secrets: Iterable[str]) -> Callable[[str], str]:
    # 先替换长的：如果一个密钥是另一个密钥的一部分，先换短的会在日志里留下长密钥的尾巴。
    # 空字符串必须跳过，否则 replace 会在每个字符之间都插入占位符。
    ordered = sorted({secret for secret in secrets if secret}, key=len, reverse=True)

    def redact(text: str) -> str:
        for secret in ordered:
            text = text.replace(secret, REDACTED)
        return text

    return redact


def _build_patcher(redact: Callable[[str], str]) -> Callable[["Record"], None]:
    def patch(record: "Record") -> None:
        message = record["message"]
        exception = record["exception"]
        if exception is not None:
            # loguru 的异常追踪是写出时才生成的，来不及过滤。所以这里自己把追踪转成文字、
            # 并进消息里一起过滤，再去掉原来的异常信息。异常消息里带着密钥并不少见
            # （比如服务端的报错原样回显了请求头）。
            trace = "".join(
                traceback.format_exception(exception.type, exception.value, exception.traceback)
            )
            message = f"{message}\n{trace.rstrip()}"
            record["exception"] = None
        record["message"] = redact(message)

    return patch


def _noop_patch(record: "Record") -> None:
    return None


def configure_logging(
    log_dir: Path,
    level: str,
    secrets: Iterable[str] = (),
    *,
    to_console: bool = True,
) -> None:
    """设置日志。可以重复调用：每次都会先撤掉旧的设置。

    标准库 logging 的日志也会转给 loguru，所以同样进日志文件、同样过密钥过滤。

    Args:
        log_dir: 日志文件夹，不存在会创建。
        level: 最低记录的级别，比如 INFO。
        secrets: 要从日志里隐藏的密钥原文。
        to_console: 是否同时输出到终端。
    """
    logger.remove()
    logger.configure(patcher=_build_patcher(_build_redactor(secrets)))
    if to_console:
        logger.add(
            sys.stderr,
            level=level,
            format=CONSOLE_FORMAT,
            backtrace=False,
            diagnose=False,
        )
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / LOG_FILE_NAME,
        level=level,
        format=FILE_FORMAT,
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        encoding="utf-8",
        # enqueue：日志先放进队列，由后台线程写文件，调用方不会被磁盘卡住
        # （见 D41 第 3 条：心跳不能被慢操作卡住）。
        enqueue=True,
        # diagnose 默认会在异常追踪里显示变量的值，可能把密钥带出来，所以关掉。
        backtrace=False,
        diagnose=False,
    )
    # 放在最后：loguru 这边都设好了，才开始接标准库的日志，不会有日志掉进还没设好的空档。
    _install_stdlib_bridge(level)


def configure_logging_for(settings: Settings, *, to_console: bool = True) -> None:
    """按配置设置日志：文件夹、级别和要隐藏的密钥都从 settings 里取。

    Args:
        settings: 已加载的全部配置。
        to_console: 是否同时输出到终端。
    """
    configure_logging(
        settings.paths.log_dir,
        settings.runtime.log_level,
        settings.secrets.secret_values(),
        to_console=to_console,
    )


def shutdown_logging() -> None:
    """关闭所有日志输出（排队的日志会先写完），撤销标准库日志的转接和密钥过滤。"""
    # 先撤转接再关 loguru：不让标准库的日志再往一个正在关闭的 loguru 里送。
    _remove_stdlib_bridge()
    logger.remove()
    logger.configure(patcher=_noop_patch)
