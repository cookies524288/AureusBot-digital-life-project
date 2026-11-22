"""安全网关 SafetyGateway：所有器官调用的唯一入口（D39、D40，06 号文档第 12 节）。

流程：权限 → 失控拦截 → 月度 token 预算 → 精力（可选的调用）→
放行并调用器官 → 把每次模型请求记进账本 → 告诉失控拦截成败；拒绝和失败都记进时间线（只有自己可见）。
安全检查本身出错（读不了账本）时当作「不允许」（06 号文档第 11 节）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from aureus.common import Clock, SafetyConfig
from aureus.organs import (
    CallAttempt,
    CallPurpose,
    OrganBus,
    OrganCallError,
    OrganRequest,
    OrganResponse,
)
from aureus.safety.errors import (
    BudgetExceededError,
    CallRejectedError,
    EnergyDepletedError,
    LedgerUnavailableError,
    PermissionDeniedError,
    RunawayBlockedError,
)
from aureus.safety.ledger import Ledger
from aureus.safety.permissions import ACTION_CALL_ORGAN, PermissionChecker
from aureus.safety.runaway_guard import RunawayGuard
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope

logger = logging.getLogger(__name__)

EVENT_CALL_REJECTED = "organ_call_rejected"
EVENT_CALL_FAILED = "organ_call_failed"


class EnergyGate(Protocol):
    """精力闸门：身体（body）实现它，网关只认这个接口，不依赖身体（D56）。"""

    def refusal_reason(self, purpose: CallPurpose) -> str | None:
        """这次调用按精力该拒绝时返回原因，放行返回 None。读不了数据时可以抛异常。"""
        ...


class SafetyGateway:
    """包着器官总线的安全网关。核心组装时只把网关交给心跳，别处拿不到总线。"""

    def __init__(
        self,
        bus: OrganBus,
        ledger: Ledger,
        timeline: TimelineDatabase,
        clock: Clock,
        config: SafetyConfig,
        permissions: PermissionChecker,
        energy_gate: EnergyGate | None = None,
    ) -> None:
        """创建网关。账本和时间线要由调用方先打开。

        没设月度上限、或关掉了失控拦截时，在这里各警告一次：
        这两个安全机制被放开是用户的选择，但不能悄悄放开，免得以后忘了（D39）。
        """
        self._bus = bus
        self._ledger = ledger
        self._timeline = timeline
        self._clock = clock
        self._config = config
        self._permissions = permissions
        self._energy_gate = energy_gate
        self._guard = RunawayGuard(config, clock)
        # 账本写入失败后置位，重启之前拒绝所有调用（D40）。
        self._ledger_write_failed = False
        # 让「检查预算 → 调用器官 → 记账」不被别的调用插进来，见 call()。
        self._call_lock = asyncio.Lock()
        if config.monthly_token_limit is None:
            logger.warning(
                "月度 token 上限没有设置，用量不受限制。在 [safety] 里写 monthly_token_limit。"
            )
        if not config.runaway_guard_enabled:
            logger.warning("失控拦截已关闭：调用频率和连续失败都不再受限制。")

    async def call(self, request: OrganRequest) -> OrganResponse:
        """经过安全检查后调用器官。

        Args:
            request: 器官请求。

        Returns:
            器官的响应。

        Raises:
            ValueError: 器官没有注册（不算调用，不进失控拦截的统计）。
            CallRejectedError: 被网关拒绝（它的三个子类：预算、失控拦截、账本不可用），
                器官没有被调用。账本在调用之后写不进去时，这次的结果也作废，抛
                LedgerUnavailableError。
            OrganCallError: 器官的所有模型都失败。
        """
        organ_id = request.organ_id
        if not self._bus.is_registered(organ_id):
            raise ValueError(f"器官 {organ_id} 未注册")

        # 为什么整段调用都在锁里：预算检查读的是账本，而账本要等调用结束才记账。
        # 中间隔着 await，不加锁的话，两个并发调用会读到同一个「已用」数字、一起通过
        # 检查、一起花钱（审查 S1，实测上限 100 用掉 290）。串行之后，上限的语义是
        # 「到点后不再发起新调用」：最多只有检查通过时正在进行的那一次会超出（D48）。
        # 代价是同一时刻只有一个器官调用；Phase 0 的心跳本来就是串行的。
        async with self._call_lock:
            return await self._call_serialized(request, lambda: self._bus.send(request))

    async def test_model(self, probe: Callable[[], Awaitable[OrganResponse]]) -> OrganResponse:
        """手动测试走同一套预算和记账，独立于后台器官的失败计数。"""
        request = OrganRequest(organ_id="model_test", prompt="", purpose=CallPurpose.USER_REPLY)
        async with self._call_lock:
            return await self._call_serialized(request, probe)

    async def _call_serialized(
        self, request: OrganRequest, invoke: Callable[[], Awaitable[OrganResponse]]
    ) -> OrganResponse:
        """持有调用锁时的完整流程：检查 → 调用器官 → 收尾。"""
        organ_id = request.organ_id
        try:
            self._admit(request)
        except CallRejectedError as rejection:
            logger.warning("%s", rejection)
            self._note_to_timeline(
                EVENT_CALL_REJECTED,
                str(rejection),
                {"organ_id": organ_id, "kind": type(rejection).__name__},
            )
            raise

        try:
            response = await invoke()
        except OrganCallError as failure:
            self._finish(organ_id, failure.attempts, succeeded=False, detail=str(failure))
            raise
        except Exception as error:
            # 器官自己出了错（代码问题）：不知道有没有花 token，账本里没有这次的记录。
            self._finish(
                organ_id,
                [],
                succeeded=False,
                detail=f"器官内部出错：{type(error).__name__}",
            )
            raise
        self._finish(organ_id, response.attempts, succeeded=True, detail="")
        return response

    def _admit(self, request: OrganRequest) -> None:
        """放行前的全部检查。任何一项不通过就抛 CallRejectedError。"""
        organ_id = request.organ_id
        # 权限放在最前面：急停中连账本都不读，更不会发起调用（D54）。
        decision = self._permissions.check(ACTION_CALL_ORGAN)
        if not decision.allowed:
            raise PermissionDeniedError(organ_id, decision.reason)

        if self._ledger_write_failed:
            raise LedgerUnavailableError(
                organ_id, "账本之前写入失败，用量可能没有记全，重启之前不再放行"
            )

        blocked_reason = self._guard.blocked_reason(organ_id)
        if blocked_reason is not None:
            raise RunawayBlockedError(organ_id, blocked_reason)

        # 不限额时也读账本：读不了就拒绝，不先花了再说。
        try:
            used = self._ledger.tokens_used_in_month(self._clock.now())
        except Exception as error:
            # 为什么捕获所有异常：安全检查出错一律当作「不允许」，不能因为没想到的错误放行。
            logger.exception("读不了账本，拒绝调用器官 %s", organ_id)
            raise LedgerUnavailableError(
                organ_id, f"读不了账本，不知道本月用了多少 token（{type(error).__name__}）"
            ) from error

        limit = self._config.monthly_token_limit
        if limit is not None and used >= limit:
            raise BudgetExceededError(organ_id, f"本月已用 {used} token，达到上限 {limit}")

        self._check_energy(request)
        self._guard.admit(organ_id)

    def _check_energy(self, request: OrganRequest) -> None:
        """精力闸门：今天的可选额度用完了就拒绝。

        闸门自己出错时当作不允许（06 号文档第 11 节）；回应用户的调用不会被问到。
        """
        if self._energy_gate is None or request.purpose is CallPurpose.USER_REPLY:
            return
        try:
            reason = self._energy_gate.refusal_reason(request.purpose)
        except Exception as error:
            logger.exception("读不了精力，拒绝可选的调用 %s", request.organ_id)
            raise EnergyDepletedError(
                request.organ_id, f"读不了精力，可选的调用先不放行（{type(error).__name__}）"
            ) from error
        if reason is not None:
            raise EnergyDepletedError(request.organ_id, reason)

    def _finish(
        self, organ_id: str, attempts: Sequence[CallAttempt], *, succeeded: bool, detail: str
    ) -> None:
        """调用结束后的收尾：告诉失控拦截成败，失败记时间线，用量记账本。

        Raises:
            LedgerUnavailableError: 账本写不进去。
        """
        self._guard.report(organ_id, succeeded=succeeded)
        if not succeeded:
            self._note_to_timeline(EVENT_CALL_FAILED, detail, {"organ_id": organ_id})
        try:
            self._ledger.record(organ_id, attempts)
        except Exception as error:
            self._ledger_write_failed = True
            logger.exception("账本写入失败，器官 %s 这次的用量没有记上，网关已锁定", organ_id)
            reason = f"账本写入失败，这次的结果作废，重启之前不再放行（{type(error).__name__}）"
            self._note_to_timeline(
                EVENT_CALL_REJECTED,
                reason,
                {"organ_id": organ_id, "kind": "LedgerUnavailableError"},
            )
            raise LedgerUnavailableError(organ_id, reason) from error

    def _note_to_timeline(self, event_type: str, content: str, metadata: dict[str, str]) -> None:
        """把拒绝和失败记进时间线，只有自己可见。尽力而为：写不进去只记日志。"""
        try:
            self._timeline.append(
                event_type=event_type,
                content=content,
                actor_type=ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata=metadata,
            )
        except Exception:
            # 时间线出错不能掩盖真正的结果：调用方要拿到的是拒绝或失败本身。
            logger.exception("拒绝或失败没能记进时间线：%s", content)
