"""心跳定期重查表情发送候选；参考 MaiBot 1.3.15 周期完整性检查，见 THIRD_PARTY.md。"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

from aureus.memory import EmojiStore

logger = logging.getLogger(__name__)
_CHECK_INTERVAL = timedelta(minutes=10)


class EmojiMaintenance:
    """按心跳时钟调度单个后台原件检查，检查不占用思考任务。"""

    def __init__(self, store: EmojiStore) -> None:
        self._store = store
        self._next_check: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    def maybe_start(self, now: datetime) -> None:
        """首拍开始计时；到期时只开一个文件检查任务。"""
        if self._next_check is None:
            self._next_check = now + _CHECK_INTERVAL
        elif now >= self._next_check and (self._task is None or self._task.done()):
            self._next_check = now + _CHECK_INTERVAL
            self._task = asyncio.create_task(self._refresh(), name="aureus-emoji-maintenance")

    async def wait(self) -> None:
        """关闭核心前等待已经开始的检查完成。"""
        if self._task is not None:
            await self._task

    async def _refresh(self) -> None:
        try:
            await asyncio.to_thread(self._store.refresh_sendable_pool)
        except (OSError, sqlite3.Error):
            logger.warning("表情发送候选检查未完成，保留上次结果")
