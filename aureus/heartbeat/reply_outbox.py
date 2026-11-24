"""心跳的回复外发队列，接收已确定的分段并按会话依次投递。"""

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from random import uniform
from unicodedata import name

from aureus.common import Clock, HeartbeatConfig
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.perception import Perception
from aureus.timeline import TimelineRecord

type _Address = ConversationKey | tuple[str, str]
type _Part = str | EmojiReplyPart
type _Send = Callable[[Perception, _Part, TimelineRecord, bool, int | None], bool]


@dataclass(frozen=True)
class EmojiReplyPart:
    """回复中的一份表情，按选择结果贴在文字前或后。"""

    digest: str
    before_text: bool = False


def _address(target: Perception) -> _Address:
    if target.route is not None:
        return ConversationKey.from_perception(target)
    return (target.channel, target.sender_id)


def _is_han(character: str) -> bool:
    return character == "〇" or name(character, "").startswith(
        ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")
    )


@dataclass
class _Reply:
    target: Perception
    record: TimelineRecord
    parts: deque[tuple[_Part, float]]
    on_delivered: Callable[[], None] | None
    on_failed: Callable[[], None] | None
    quote_source: bool = False
    delivered: bool = False
    text_parts: int = 0

    def fail(self) -> None:
        if not self.delivered and self.on_failed is not None:
            self.on_failed()


@dataclass
class _ConversationOutbox:
    replies: deque[_Reply]
    last_sent_at: datetime

    def seconds_until_due(self, now: datetime) -> float:
        delay = self.replies[0].parts[0][1]
        return max(0.0, delay - (now - self.last_sent_at).total_seconds())


class ReplyOutbox:
    """在心跳中投递到期的分段，等待期间只保存已记录的回复。"""

    def __init__(self, clock: Clock, config: HeartbeatConfig, send: _Send) -> None:
        self._clock = clock
        self._config = config
        self._send = send
        self._conversations: dict[_Address, _ConversationOutbox] = {}

    def _typing_delay(self, text: str) -> float:
        config = self._config
        if not config.typing_delay_enabled:
            return 0.0
        han_characters = sum(_is_han(character) for character in text)
        return round(
            han_characters * config.typing_han_seconds
            + (len(text) - han_characters) * config.typing_other_seconds
            + uniform(config.typing_jitter_min_seconds, config.typing_jitter_max_seconds),
            6,
        )

    def has_pending(self, target: Perception) -> bool:
        """查看目标会话是否还在投递之前的回复。"""
        return _address(target) in self._conversations

    def enqueue(
        self,
        target: Perception,
        record: TimelineRecord,
        parts: Sequence[str],
        on_delivered: Callable[[], None] | None,
        on_failed: Callable[[], None] | None,
        *,
        emoji: EmojiReplyPart | None = None,
        quote_source: bool = False,
    ) -> bool:
        """同一次回复的后续段计入打字时间，并立即投递已到期的段。"""
        address = _address(target)
        queue = self._conversations.get(address)
        if queue is None:
            queue = self._conversations[address] = _ConversationOutbox(deque(), self._clock.now())
        timed_parts: deque[tuple[_Part, float]] = deque(
            (part, self._typing_delay(part) if index else 0.0) for index, part in enumerate(parts)
        )
        if emoji is not None:
            if emoji.before_text:
                timed_parts.appendleft((emoji, 0.0))
            else:
                timed_parts.append((emoji, 0.0))
        reply = _Reply(target, record, timed_parts, on_delivered, on_failed, quote_source)
        queue.replies.append(reply)
        self._deliver_due(address, queue)
        return reply.delivered

    def clear(self) -> None:
        """停机或急停时丢弃待发分段。"""
        conversations = self._conversations
        self._conversations = {}
        for queue in conversations.values():
            for reply in queue.replies:
                reply.fail()

    def deliver_due(self) -> None:
        """每个会话都可独立发送已到时间的内容。"""
        for address, queue in tuple(self._conversations.items()):
            self._deliver_due(address, queue)

    def seconds_until_due(self) -> float:
        """最早的待发送时间，用于提前叫醒心跳。"""
        now = self._clock.now()
        return min(
            (queue.seconds_until_due(now) for queue in self._conversations.values()),
            default=float("inf"),
        )

    def _deliver_due(self, address: _Address, queue: _ConversationOutbox) -> None:
        while queue.replies and queue.seconds_until_due(self._clock.now()) == 0:
            reply = queue.replies[0]
            content, _ = reply.parts.popleft()
            segment = None
            if isinstance(content, str):
                reply.text_parts += 1
                segment = reply.text_parts
            delivered = self._send(reply.target, content, reply.record, reply.quote_source, segment)
            reply.quote_source = False
            queue.last_sent_at = self._clock.now()
            if not reply.parts:
                queue.replies.popleft()
            if isinstance(content, str) and delivered and not reply.delivered:
                reply.delivered = True
                if reply.on_delivered is not None:
                    reply.on_delivered()
            if not reply.parts:
                reply.fail()
        if not queue.replies:
            self._conversations.pop(address, None)
