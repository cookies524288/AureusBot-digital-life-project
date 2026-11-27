"""正文与平台回执各自到达后，只把仍合适的一张晚到表情交给原出站队列。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from aureus.common import Clock
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.reply_outbox import EmojiReplyPart

type Selection = asyncio.Task[EmojiReplyPart | None]
type StartSelection = Callable[[], Selection | None]


@dataclass(eq=False)
class StickerRound:
    """一次补图需要等齐的正文回执、合适期限和选图结果。"""

    deadline: datetime
    permitted: Callable[[], bool]
    deliver: Callable[[EmojiReplyPart], None]
    start: StartSelection | None
    selection: Selection | None
    remaining: int


class StickerFollowups:
    """每个会话至多保留一张未送出的补图，不阻塞正文或下一轮。"""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.active: dict[ConversationKey, StickerRound] = {}

    def add(self, key: ConversationKey, item: StickerRound) -> Callable[[int | None, bool], None]:
        """登记新一轮并返回逐段回执处理器；任何一段失败都不补图。"""
        self.cancel(key)
        self.active[key] = item
        if item.selection is not None:
            self._watch(key, item)

        def settled(segment: int | None, success: bool) -> None:
            if segment is None or self.active.get(key) is not item:
                return
            if not success:
                self.cancel(key)
                return
            item.remaining -= 1
            asyncio.get_running_loop().call_soon(self._check, key, item)

        return settled

    def _watch(self, key: ConversationKey, item: StickerRound) -> None:
        assert item.selection is not None
        item.selection.add_done_callback(lambda _: self._check(key, item))

    def _check(self, key: ConversationKey, item: StickerRound) -> None:
        if self.active.get(key) is not item:
            return
        if self.clock.now() >= item.deadline or not item.permitted():
            self.cancel(key)
            return
        if item.remaining:
            return
        if item.selection is None and item.start is not None:
            item.selection, item.start = item.start(), None
            if item.selection is not None:
                self._watch(key, item)
        if item.selection is not None and not item.selection.done():
            return
        self.active.pop(key)
        if (
            item.selection is not None
            and not item.selection.cancelled()
            and not item.selection.exception()
            and (emoji := item.selection.result()) is not None
        ):
            item.deliver(emoji)

    def tick(self) -> None:
        """依项目时钟复查回执、选图和会话权限。"""
        for key, item in tuple(self.active.items()):
            self._check(key, item)

    def cancel(self, key: ConversationKey) -> None:
        """在插话或放弃会话时取消其尚未送出的表情。"""
        item = self.active.pop(key, None)
        if item is not None and item.selection is not None:
            item.selection.cancel()

    def clear(self) -> None:
        """停机或急停时取消所有会话尚未送出的表情。"""
        for key in tuple(self.active):
            self.cancel(key)
