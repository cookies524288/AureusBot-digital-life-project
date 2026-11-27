"""D148：同一份生效模型配置下，业务器官按服务商共用短期熔断状态。"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger

from aureus.common.clock import Clock
from aureus.organs.model_config import CircuitBreakerConfig, ModelRoute


class ProviderCircuitOpenError(Exception):
    """本次没有发送请求：服务商正在冷却或已经有探测在途。"""


@dataclass
class _ProviderState:
    """一个服务商当前的连续失败数与探测状态。"""

    failures: int = 0
    retry_at: datetime | None = None
    probing: bool = False


@dataclass
class ProviderAttempt:
    """由请求调用者设置真实成败；取消、尚未发送不改变失败数。"""

    succeeded: bool | None = None


class ProviderCircuitBreaker:
    """读真实请求成败，影响之后的同名路线；配置可关闭，状态不持久化。"""

    def __init__(self, config: CircuitBreakerConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._states: dict[str, _ProviderState] = {}

    @contextmanager
    def attempt(self, organ_id: str, route: ModelRoute) -> Iterator[ProviderAttempt]:
        """在发送前取得许可；半开时，同一服务商只让一次请求在途。"""
        attempt = ProviderAttempt()
        if not self._config.enabled:
            yield attempt
            return
        provider = route.provider.name
        state = self._states.setdefault(provider, _ProviderState())
        if state.retry_at is not None:
            reason = None
            if self._clock.now() < state.retry_at:
                reason = "服务商熔断冷却中"
            elif state.probing:
                reason = "服务商熔断探测在途"
            if reason is not None:
                logger.bind(
                    event="provider_circuit_skip",
                    organ_id=organ_id,
                    provider=provider,
                    model=route.model.name,
                    reason=reason,
                ).warning("器官 {} 跳过服务商 {}：{}", organ_id, provider, reason)
                raise ProviderCircuitOpenError(reason)
            state.probing = True
        try:
            yield attempt
        finally:
            self._finish(provider, state, attempt.succeeded)

    def _finish(self, provider: str, state: _ProviderState, succeeded: bool | None) -> None:
        if self._states[provider] is not state:
            # 旧请求虽然照常记账，但不能替代本次冷却后的探测。
            return
        state.probing = False
        if succeeded is None:
            return
        if succeeded:
            state.failures = 0
            state.retry_at = None
            return
        state.failures += 1
        if state.failures >= self._config.failure_threshold:
            retry_at = self._clock.now() + timedelta(seconds=self._config.cooldown_seconds)
            self._states[provider] = _ProviderState(state.failures, retry_at)
