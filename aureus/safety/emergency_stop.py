"""急停：数据根目录里的一个标记文件（06 号文档第 7 节，D54）。

文件在就是急停中，不在就是正常。用文件而不是进程内的开关，是因为设它的常常是另一个进程：
用户在另一个终端敲 `aureus halt`，正在运行的心跳下一拍就能看见。
读标记出错时当作急停中：安全检查读不了的时候朝安全方向倒（06 号文档第 11 节）。
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureus.common.clock import Clock, ensure_aware

logger = logging.getLogger(__name__)

# 用户没写原因时记的话。
DEFAULT_HALT_REASON = "用户要求急停"

_KEY_AT = "at"
_KEY_REASON = "reason"


@dataclass(frozen=True)
class HaltStatus:
    """急停状态。halted 为 False 时，since 和 reason 没有意义。"""

    halted: bool
    since: datetime | None = None  # 什么时候设的；标记文件读不懂时是 None
    reason: str = ""


class EmergencyStop:
    """急停标记文件的读、写、删。读很便宜（没急停时只是看一眼文件在不在），心跳每拍都读。"""

    def __init__(self, marker_file: Path, clock: Clock) -> None:
        """创建急停开关。

        Args:
            marker_file: 标记文件的位置（AureusPaths.halt_file）。
            clock: 时钟；记录急停是什么时候设的。
        """
        self._marker_file = marker_file
        self._clock = clock

    def status(self) -> HaltStatus:
        """现在是不是急停中。不抛异常：读不了就当作急停中。"""
        try:
            text = self._marker_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return HaltStatus(halted=False)
        except OSError as error:
            logger.error("读不了急停标记 %s，按急停中处理：%s", self._marker_file, error)
            return HaltStatus(halted=True, reason=f"读不了急停标记（{type(error).__name__}）")
        return _parse_marker(text)

    def engage(self, reason: str = "") -> bool:
        """设急停。已经在急停中时不改动原来的记录。

        Returns:
            这次新设的返回 True；本来就在急停中返回 False。

        Raises:
            OSError: 标记文件写不进去（调用方要告诉用户急停没有生效）。
        """
        if self._marker_file.exists():
            return False
        content = json.dumps(
            {
                _KEY_AT: ensure_aware(self._clock.now()).isoformat(),
                _KEY_REASON: reason.strip() or DEFAULT_HALT_REASON,
            },
            ensure_ascii=False,
        )
        self._marker_file.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：读的一方不会读到写了一半的标记（读到半截会被当成急停，
        # 也安全，但原因就丢了）。
        temporary = self._marker_file.with_name(self._marker_file.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, self._marker_file)
        return True

    def release(self) -> bool:
        """解除急停。

        Returns:
            这次解除的返回 True；本来就不在急停中返回 False。

        Raises:
            OSError: 标记文件删不掉（调用方要告诉用户急停还在）。
        """
        try:
            self._marker_file.unlink()
        except FileNotFoundError:
            return False
        return True


def _parse_marker(text: str) -> HaltStatus:
    """读懂标记文件。内容坏了也算急停中，只是不知道原因和时间。"""
    try:
        data = json.loads(text)
        reason = str(data[_KEY_REASON])
        since = datetime.fromisoformat(data[_KEY_AT])
    except (ValueError, KeyError, TypeError):
        return HaltStatus(halted=True, reason="急停标记的内容读不懂")
    return HaltStatus(halted=True, since=since, reason=reason)
