"""通道子系统 channels：与外界接触的途径。Phase 0 只有终端（03 号文档“通道”一节）。

通道依赖心跳（感知和消息的格式由心跳定义），心跳不依赖通道（D41 第 7 条）。
"""

from .terminal import CHANNEL_NAME, TERMINAL_USER_ID, TerminalChannel

__all__ = ["CHANNEL_NAME", "TERMINAL_USER_ID", "TerminalChannel"]
