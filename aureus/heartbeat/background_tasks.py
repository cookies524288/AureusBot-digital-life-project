"""只监督可安全重跑的后台索引构建（3.66 D2）。"""

import logging
from asyncio import sleep
from collections.abc import Awaitable, Callable
from typing import Literal

from aureus.organs import OrganCallError

type IndexKind = Literal["expression", "emoji_cache", "images"]
type IndexBatch = Callable[[], Awaitable[tuple[bool, bool]]]
_BACKOFF_SECONDS = (1.0, 5.0, 30.0)
logger = logging.getLogger(__name__)


class IndexSupervisor:
    """同一次后台构建中，每种索引只重启三次；成功的批次不重置次数。"""

    def __init__(self) -> None:
        self._restarts: dict[IndexKind, int] = {}

    async def run_batch(self, kind: IndexKind, build: IndexBatch) -> tuple[bool, bool]:
        """重复一批可安全补建的索引；熔断和状态错误原样交给调用者。"""
        while True:
            try:
                return await build()
            except OrganCallError as error:
                if error.skipped_by_circuit:
                    raise
                used = self._restarts.get(kind, 0)
                if used == len(_BACKOFF_SECONDS):
                    logger.exception("后台索引 %s 已用完 %s 次重启机会", kind, used)
                    raise
                delay = _BACKOFF_SECONDS[used]
                self._restarts[kind] = used + 1
                logger.exception("后台索引 %s 失败，%s 秒后第 %s 次重启", kind, delay, used + 1)
            await sleep(delay)
