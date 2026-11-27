"""网页实时事件：心跳、收件箱和时间线把变化交给网页，网页自己决定刷新哪一页（3.43）。

事件只带类型和定位用的最小标识，不带聊天正文：网页收到后按需重新拉取，权限仍由
原来的接口检查。断线重连后的补齐也由网页负责，这里不保存、不重放历史事件。

发布方和订阅方都在同一个事件循环里。订阅方只往自己的队列里放，不能阻塞；某个订阅者
出错只记日志，不影响别的网页，更不影响心跳（任何通道出问题都不影响核心）。
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 事件类型（topic）：网页按它决定让哪一页的缓存失效。
TOPIC_INBOX = "inbox"  # 新的平台消息已存进收件箱
TOPIC_CHAT = "chat"
TOPIC_TIMELINE = "timeline"  # 时间线追加了一条记录
TOPIC_ROUND = "round"  # 正在回复谁的状态变了
TOPIC_RESOURCES = "resources"  # 学习或记忆资源变了

# 资源事件里的资源名：网页按它刷新对应页面。
RESOURCE_LANGUAGE = "language"
RESOURCE_EXPRESSION = "expression"
RESOURCE_EMOJI = "emoji"
RESOURCE_IMAGES = "images"
RESOURCE_MEMORY = "memory"


@dataclass(frozen=True)
class LiveEvent:
    """一条推给网页的轻量事件：类型加最小标识，不放正文。"""

    topic: str
    data: Mapping[str, object] = field(default_factory=dict)


class LiveEventHub:
    """网页实时事件总线：发布方交给它，连着并订阅了的网页各收一份。

    急停、维护暂停或关闭时由心跳调用 pause()：暂停期间的事件直接丢掉，恢复后由下一次
    状态变化补上当前状态（不重放暂停期间的变更）。
    """

    def __init__(self) -> None:
        self._subscribers: dict[int, Callable[[LiveEvent], None]] = {}
        self._next_key = 0
        self._paused = False

    @property
    def paused(self) -> bool:
        """暂停时不发布新事件；已经排队的由网页那边的发送循环丢掉。"""
        return self._paused

    @property
    def subscriber_count(self) -> int:
        """现在有几个网页订阅了实时事件。"""
        return len(self._subscribers)

    def subscribe(self, subscriber: Callable[[LiveEvent], None]) -> Callable[[], None]:
        """登记一个订阅者。

        Returns:
            取消订阅的函数；网页断开时调用，重复调用没有影响。
        """
        key = self._next_key
        self._next_key += 1
        self._subscribers[key] = subscriber

        def unsubscribe() -> None:
            self._subscribers.pop(key, None)

        return unsubscribe

    def publish(self, event: LiveEvent) -> None:
        """把事件交给所有订阅者；某个订阅者出错只记日志，不影响别人和发布方。"""
        if self._paused:
            return
        for subscriber in list(self._subscribers.values()):
            try:
                subscriber(event)
            except Exception:
                logger.exception("网页实时事件有一个订阅者没能收下")

    def pause(self) -> None:
        """停止发布：急停、维护暂停或关闭时用。"""
        self._paused = True

    def resume(self) -> None:
        """恢复发布；暂停期间的事件不补发。"""
        self._paused = False
