"""权限等级检查（06 号文档第 2 节，D54）。

每种行动在这里登记一个等级，做之前由代码检查，不靠提示词劝大模型自觉。规则：
- 没登记的行动一律拒绝：新增行动时必须先决定它的等级；
- L4 永远拒绝；L3 要用户批准，批准流程（Phase 2）上线之前一律拒绝；
- 急停中拒绝 L1 以上的所有行动，只放行 L0（只影响自己内部的，比如写时间线）；
- 检查本身出错也当作不允许（06 号文档第 11 节）。
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType

from aureus.safety.emergency_stop import EmergencyStop

logger = logging.getLogger(__name__)

# 行动名。心跳和网关用这些常量，别处新增行动时也先在这里登记。
ACTION_CALL_ORGAN = "call_organ"  # 调用器官（会联网、花 token）
ACTION_REPLY_TO_USER = "reply_to_user"  # 回复用户的消息


class PermissionLevel(IntEnum):
    """权限等级，数字越大限制越多（06 号文档第 2 节的表）。"""

    FREE = 0  # L0 自由：只影响它自己内部
    ROUTINE = 1  # L1 常规：日常行为，记录即可
    TRACEABLE = 2  # L2 留痕可撤：必须记录、能撤销
    NEEDS_APPROVAL = 3  # L3 需批准：影响大或不可逆
    FORBIDDEN = 4  # L4 禁止：任何情况都不允许


DEFAULT_ACTION_LEVELS: Mapping[str, PermissionLevel] = MappingProxyType(
    {
        # 调用器官等于联网并花钱，但对象是自己配置的模型服务：算日常行为。
        # 放在 L1 而不是 L0，是为了让急停能拦住它（急停只放行 L0）。
        ACTION_CALL_ORGAN: PermissionLevel.ROUTINE,
        ACTION_REPLY_TO_USER: PermissionLevel.ROUTINE,
    }
)


@dataclass(frozen=True)
class PermissionDecision:
    """一次检查的结果。拒绝时 reason 是说给用户听的中文。"""

    allowed: bool
    reason: str = ""


ALLOWED = PermissionDecision(allowed=True)


class PermissionChecker:
    """权限检查器。心跳和安全网关共用同一个，保证两边看到的急停状态一致。"""

    def __init__(
        self,
        emergency_stop: EmergencyStop,
        levels: Mapping[str, PermissionLevel] = DEFAULT_ACTION_LEVELS,
    ) -> None:
        """创建检查器。

        Args:
            emergency_stop: 急停开关。
            levels: 行动名到等级的登记表；默认用 DEFAULT_ACTION_LEVELS，测试时可以传别的。
        """
        self._emergency_stop = emergency_stop
        self._levels = levels

    def check(self, action: str) -> PermissionDecision:
        """这个行动现在能不能做。不抛异常：出任何错都返回拒绝。"""
        try:
            return self._decide(action)
        except Exception:
            # 为什么捕获所有异常：安全检查出错一律当作「不允许」，不能因为没想到的错误放行。
            logger.exception("检查行动 %s 的权限时出错，按拒绝处理", action)
            return PermissionDecision(allowed=False, reason="权限检查出错，不放行")

    def _decide(self, action: str) -> PermissionDecision:
        level = self._levels.get(action)
        if level is None:
            return PermissionDecision(
                allowed=False, reason=f"行动 {action} 没有登记权限等级，不放行"
            )
        if level is PermissionLevel.FORBIDDEN:
            return PermissionDecision(allowed=False, reason=f"行动 {action} 是 L4，任何情况都禁止")
        if level is PermissionLevel.NEEDS_APPROVAL:
            return PermissionDecision(
                allowed=False, reason=f"行动 {action} 是 L3，需要用户批准，批准流程还没有上线"
            )
        if level > PermissionLevel.FREE:
            halt = self._emergency_stop.status()
            if halt.halted:
                return PermissionDecision(allowed=False, reason=f"急停中：{halt.reason}")
        return ALLOWED
