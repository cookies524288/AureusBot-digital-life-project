"""通道子系统 channels：与外界接触的途径。目前有终端和网页（03 号文档“通道”一节）。

通道依赖心跳（感知和消息的格式由心跳定义），心跳不依赖通道（D41 第 7 条）。
"""

from .onebot import QQ_CHANNEL_NAME
from .qq import QQChannel
from .terminal import CHANNEL_NAME, TERMINAL_USER_ID, TerminalChannel
from .web import CHANNEL_NAME as WEB_CHANNEL_NAME
from .web import WEB_USER_ID, WebChannel

__all__ = [
    "CHANNEL_NAME",
    "QQ_CHANNEL_NAME",
    "TERMINAL_USER_ID",
    "WEB_CHANNEL_NAME",
    "WEB_USER_ID",
    "QQChannel",
    "TerminalChannel",
    "WebChannel",
]
