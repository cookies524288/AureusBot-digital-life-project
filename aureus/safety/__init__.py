"""安全子系统 safety：器官调用的账本、月度 token 预算和失控拦截（D39、D40）。

所有器官调用都经过 `SafetyGateway`。本包依赖 organs 和 timeline，反过来不行（D40）。
"""

from .emergency_stop import DEFAULT_HALT_REASON, EmergencyStop, HaltStatus
from .errors import (
    BudgetExceededError,
    CallRejectedError,
    LedgerUnavailableError,
    PermissionDeniedError,
    RunawayBlockedError,
)
from .gateway import EVENT_CALL_FAILED, EVENT_CALL_REJECTED, SafetyGateway
from .ledger import Ledger, LedgerEntry
from .permissions import (
    ACTION_CALL_ORGAN,
    ACTION_REPLY_TO_USER,
    DEFAULT_ACTION_LEVELS,
    PermissionChecker,
    PermissionDecision,
    PermissionLevel,
)
from .runaway_guard import RunawayGuard

__all__ = [
    "ACTION_CALL_ORGAN",
    "ACTION_REPLY_TO_USER",
    "DEFAULT_ACTION_LEVELS",
    "DEFAULT_HALT_REASON",
    "EVENT_CALL_FAILED",
    "EVENT_CALL_REJECTED",
    "BudgetExceededError",
    "CallRejectedError",
    "EmergencyStop",
    "HaltStatus",
    "Ledger",
    "LedgerEntry",
    "LedgerUnavailableError",
    "PermissionChecker",
    "PermissionDecision",
    "PermissionDeniedError",
    "PermissionLevel",
    "RunawayBlockedError",
    "RunawayGuard",
    "SafetyGateway",
]
