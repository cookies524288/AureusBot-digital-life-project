"""网页通道 web：网页上的话交给心跳，心跳交回的消息转给连着的网页（路线图 1.10，D63 第 8 条）。

通道只做翻译：不做决定、不存长期状态，网页断开不影响核心（03 号文档「通道」一节）。
这里不碰网络：怎么连上浏览器（HTTP、WebSocket）由 aureus.web 负责，它在最上层，
通过 subscribe 接收消息、通过 send_from_user 递话进来，所以通道可以用假心跳单独测试。

登录的检查在 aureus.web：能走到这里的话，一定是已经登录的用户说的，
所以信任等级是 VERIFIED_USER，和本机终端一样（D63 第 8 条）。
"""

import logging
from collections.abc import Callable
from typing import Protocol

from aureus.heartbeat import OutgoingMessage, Perception, TrustLevel, VisibilityScope

logger = logging.getLogger(__name__)

CHANNEL_NAME = "web"
# 网页登录的只有用户本人，账号固定；核心组装时登记为用户的账号（D63 第 8 条）。
WEB_USER_ID = "local"
# 一条消息最多多少字：超长的话会撑满上下文、多花 token。数字是按「聊天一条消息
# 远用不到」估的，没有实测依据。
MAX_MESSAGE_CHARS = 4000

# 收消息的函数：必须很快返回、不能阻塞（心跳会直接调用它）。
Subscriber = Callable[[OutgoingMessage], None]


class HeartbeatHandle(Protocol):
    """网页通道用到的心跳接口。"""

    def submit(self, perception: Perception) -> bool:
        """交一条感知给心跳。"""
        ...


class WebChannel:
    """网页通道。先 attach 到心跳上（attach_channel(CHANNEL_NAME, channel.show)）。"""

    def __init__(self, heartbeat: HeartbeatHandle) -> None:
        self._heartbeat = heartbeat
        self._subscribers: dict[int, Subscriber] = {}
        self._next_key = 0

    @property
    def user_visibility(self) -> VisibilityScope:
        """网页登录用户的对话范围；回复上下文和网页时间线共用（D84）。"""
        return VisibilityScope.USER_ONLY

    @property
    def subscriber_count(self) -> int:
        """现在有几个网页连着。"""
        return len(self._subscribers)

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """有网页连上来：以后发往网页的消息也交给它。

        Returns:
            取消订阅的函数；网页断开时调用，重复调用没有影响。
        """
        key = self._next_key
        self._next_key += 1
        self._subscribers[key] = subscriber

        def unsubscribe() -> None:
            self._subscribers.pop(key, None)

        return unsubscribe

    def show(self, message: OutgoingMessage) -> None:
        """心跳交回的消息：转给所有连着的网页。这个函数要交给 Heartbeat.attach_channel。

        某个网页出错只记日志，不影响别的网页，更不影响心跳（任何通道出问题都不影响核心）。
        """
        for subscriber in list(self._subscribers.values()):
            try:
                subscriber(message)
            except Exception:
                logger.exception("网页通道有一个订阅者没能收下消息")

    def send_from_user(self, text: str) -> bool:
        """网页上登录的用户说了一句话，交给心跳。要在事件循环里调用。

        Returns:
            收下了返回 True；心跳正在关闭、不再收时返回 False。

        Raises:
            ValueError: 消息是空的，或者超过 MAX_MESSAGE_CHARS 个字。
        """
        content = text.strip()
        if not content:
            raise ValueError("消息不能是空的")
        if len(content) > MAX_MESSAGE_CHARS:
            raise ValueError(f"消息太长了，最多 {MAX_MESSAGE_CHARS} 个字")
        return self._heartbeat.submit(
            Perception(
                channel=CHANNEL_NAME,
                sender_id=WEB_USER_ID,
                content=content,
                visibility=self.user_visibility,
                trust=TrustLevel.VERIFIED_USER,
            )
        )
