"""安全子系统 safety：器官调用的账本、月度 token 预算和失控拦截（D39、D40）。

所有器官调用都经过 `SafetyGateway`。本包依赖 organs 和 timeline，反过来不行（D40）。
"""

from .errors import (
    BudgetExceededError,
    CallRejectedError,
    LedgerUnavailableError,
    RunawayBlockedError,
)
from .gateway import EVENT_CALL_FAILED, EVENT_CALL_REJECTED, SafetyGateway
from .ledger import Ledger, LedgerEntry, month_bounds
from .runaway_guard import RunawayGuard

__all__ = [
    "EVENT_CALL_FAILED",
    "EVENT_CALL_REJECTED",
    "BudgetExceededError",
    "CallRejectedError",
    "Ledger",
    "LedgerEntry",
    "LedgerUnavailableError",
    "RunawayBlockedError",
    "RunawayGuard",
    "SafetyGateway",
    "month_bounds",
]
