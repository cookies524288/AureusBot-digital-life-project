"""心跳的回复外发队列，接收已确定的分段并按会话依次投递。"""

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from random import uniform
from unicodedata import name

from aureus.common import Clock, HeartbeatConfig
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.perception import Perception, QQChatAction
from aureus.heartbeat.round_timing import RoundTiming
from aureus.timeline import TimelineRecord

type _Address = ConversationKey | tuple[str, str]
type _Part = str | EmojiReplyPart | QQChatAction | AtReplyPart
type _DeliveryGuard = Callable[[], bool]
type PartReceipt = Callable[[int | None, bool], None]
type _Send = Callable[
    [
        Perception,
        _Part,
        TimelineRecord,
        bool,
        int | None,
        _DeliveryGuard | None,
        PartReceipt | None,
        RoundTiming | None,
    ],
    bool,
]


@dataclass(frozen=True)
class EmojiReplyPart:
    """回复中的一份表情，按选择结果贴在文字前或后。"""

    digest: str
    before_text: bool = False


@dataclass(frozen=True)
class AtReplyPart:
    """同一首段的提及和文字，只创建一个平台气泡。"""

    text: str
    action: QQChatAction


def _address(target: Perception) -> _Address:
    if target.route is not None:
        return ConversationKey.from_perception(target)
    return (target.channel, target.sender_id)


def _is_han(character: str) -> bool:
    return character == "〇" or name(character, "").startswith(
        ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")
    )


@dataclass(eq=False)
class _Reply:
    target: Perception
    record: TimelineRecord
    parts: deque[tuple[_Part, float]]
    on_delivered: Callable[[], None] | None
    on_failed: Callable[[], None] | None
    can_deliver: _DeliveryGuard | None
    quote_source: bool = False
    delivered: bool = False
    text_parts: int = 0
    on_receipt: PartReceipt | None = None
    mention_confirmed: bool | None = True
    track_receipts: bool = False
    on_activity: Callable[[], None] | None = None
    timing: RoundTiming | None = None

    def fail(self) -> None:
        if not self.delivered and self.on_failed is not None:
            self.on_failed()


@dataclass
class _ConversationOutbox:
    replies: deque[_Reply]
    last_sent_at: datetime

    def seconds_until_due(self, now: datetime) -> float:
        reply = self.replies[0]
        if reply.mention_confirmed is None:
            return float("inf")
        if not reply.mention_confirmed:
            return 0.0
        delay = reply.parts[0][1]
        return max(0.0, delay - (now - self.last_sent_at).total_seconds())


class ReplyOutbox:
    """在心跳中投递到期的分段，等待期间只保存已记录的回复。"""

    def __init__(
        self,
        clock: Clock,
        config: HeartbeatConfig,
        send: _Send,
        record_discard: Callable[[TimelineRecord, int | None], None],
        wake: Callable[[], None],
    ) -> None:
        self._clock = clock
        self._config = config
        self._send = send
        self._record_discard = record_discard
        self._wake = wake
        self._conversations: dict[_Address, _ConversationOutbox] = {}
        self._awaiting_receipt: dict[_Address, int] = {}

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

    def has_pending_conversation(self, key: ConversationKey) -> bool:
        """查看这个平台会话是否还有没发完的分段。"""
        return key in self._conversations or key in self._awaiting_receipt

    def enqueue(
        self,
        target: Perception,
        record: TimelineRecord,
        parts: Sequence[str],
        on_delivered: Callable[[], None] | None,
        on_failed: Callable[[], None] | None,
        *,
        emoji: EmojiReplyPart | None = None,
        qq_action: QQChatAction | None = None,
        quote_source: bool = False,
        can_deliver: _DeliveryGuard | None = None,
        on_receipt: PartReceipt | None = None,
        track_receipts: bool = False,
        on_activity: Callable[[], None] | None = None,
        timing: RoundTiming | None = None,
    ) -> bool:
        """同一次回复的后续段计入打字时间，并立即投递已到期的段。"""
        if not parts and emoji is None and qq_action is None:
            return False
        if qq_action is not None and (emoji is not None or (qq_action.kind == "at") != bool(parts)):
            return False
        address = _address(target)
        queue = self._conversations.get(address)
        if queue is None:
            queue = self._conversations[address] = _ConversationOutbox(deque(), self._clock.now())
        timed_parts: deque[tuple[_Part, float]] = deque(
            (part, self._typing_delay(part) if index else 0.0) for index, part in enumerate(parts)
        )
        if qq_action is not None:
            if qq_action.kind == "at":
                timed_parts[0] = (AtReplyPart(parts[0], qq_action), 0.0)
            else:
                timed_parts.append((qq_action, 0.0))
        if emoji is not None:
            if emoji.before_text:
                timed_parts.appendleft((emoji, 0.0))
            else:
                timed_parts.append((emoji, 0.0))
        reply = _Reply(
            target,
            record,
            timed_parts,
            on_delivered,
            on_failed,
            can_deliver,
            quote_source,
            on_receipt=on_receipt,
            track_receipts=track_receipts,
            on_activity=on_activity,
            timing=timing,
        )
        queue.replies.append(reply)
        self._deliver_due(address, queue)
        if reply in queue.replies and on_activity is not None:
            on_activity()
        return reply.delivered

    def clear(self) -> None:
        """停机或急停时丢弃待发分段。"""
        conversations = self._conversations
        self._conversations = {}
        for queue in conversations.values():
            for reply in queue.replies:
                self._discard(reply)

    def _discard(self, reply: _Reply) -> None:
        text_parts = reply.text_parts
        for part, _ in reply.parts:
            segment = None
            if isinstance(part, (str, AtReplyPart)):
                text_parts += 1
                segment = text_parts
            self._record_discard(reply.record, segment)
            if reply.on_receipt is not None:
                reply.on_receipt(segment, False)
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

    def _mention_receipt(self, reply: _Reply) -> PartReceipt:
        def receipt(segment: int | None, accepted: bool) -> None:
            reply.mention_confirmed = accepted
            self._wake()
            if reply.on_receipt is not None:
                reply.on_receipt(segment, accepted)

        return receipt

    def _track_receipt(
        self, address: _Address, reply: _Reply, notify: PartReceipt | None
    ) -> PartReceipt:
        """交给通道不等于发完；实际回执才能释放驻留投递。"""
        self._awaiting_receipt[address] = self._awaiting_receipt.get(address, 0) + 1

        def receipt(segment: int | None, accepted: bool) -> None:
            remaining = self._awaiting_receipt[address] - 1
            if remaining:
                self._awaiting_receipt[address] = remaining
            else:
                del self._awaiting_receipt[address]
            if accepted and reply.on_activity is not None:
                reply.on_activity()
            self._wake()
            if notify is not None:
                notify(segment, accepted)

        return receipt

    def _deliver_due(self, address: _Address, queue: _ConversationOutbox) -> None:
        while queue.replies:
            reply = queue.replies[0]
            if reply.mention_confirmed is False or (
                reply.can_deliver is not None and not reply.can_deliver()
            ):
                queue.replies.popleft()
                self._discard(reply)
                continue
            if queue.seconds_until_due(self._clock.now()) != 0:
                break
            content, _ = reply.parts.popleft()
            segment = None
            if isinstance(content, (str, AtReplyPart)):
                reply.text_parts += 1
                segment = reply.text_parts
            receipt = reply.on_receipt
            if isinstance(content, AtReplyPart) and reply.parts:
                reply.mention_confirmed = None
                receipt = self._mention_receipt(reply)
            if reply.track_receipts:
                receipt = self._track_receipt(address, reply, receipt)
            delivered = self._send(
                reply.target,
                content,
                reply.record,
                reply.quote_source,
                segment,
                reply.can_deliver,
                receipt,
                reply.timing,
            )
            if delivered and not reply.track_receipts and reply.on_activity is not None:
                reply.on_activity()
            reply.quote_source = False
            queue.last_sent_at = self._clock.now()
            if not reply.parts:
                queue.replies.popleft()
            if isinstance(content, (str, AtReplyPart)) and delivered and not reply.delivered:
                reply.delivered = True
                if reply.on_delivered is not None:
                    reply.on_delivered()
            if not reply.parts:
                reply.fail()
        if not queue.replies:
            self._conversations.pop(address, None)
