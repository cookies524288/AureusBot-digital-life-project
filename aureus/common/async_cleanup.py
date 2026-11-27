"""异步收尾时保留运行错误和收尾错误。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


@asynccontextmanager
async def finish_after(finish: Callable[[], Awaitable[None]] | None) -> AsyncIterator[None]:
    """无论运行成功还是失败都收尾；两者都出错时保留两个异常。"""
    try:
        yield
    except BaseException as error:
        if finish is not None:
            try:
                await finish()
            except BaseException as close_error:
                raise BaseExceptionGroup("运行和收尾均出错", [error, close_error]) from None
        raise
    else:
        if finish is not None:
            await finish()


async def wait_for_cleanup(task: asyncio.Task[None]) -> None:
    """取消等待方时仍等已经开始的有界收尾，并保留收尾自己的异常。"""
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.cancelled():
            async with finish_after(lambda: task):
                raise
        raise
