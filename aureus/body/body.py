"""身体 Body：读真实来源，算出身体信号（04 号文档，D56）。

首批只有精力。身体只读：账本和时钟是它的来源，它不写任何状态。
同时它是安全网关的「精力闸门」：每日额度花完后，可选的调用被拒绝，回应用户的不受影响。
"""

import logging

from aureus.body.energy import UNLIMITED_READING, EnergyReading, compute_energy
from aureus.common import BodyConfig, BudgetCalendar, Clock
from aureus.organs import CallPurpose
from aureus.safety import Ledger

logger = logging.getLogger(__name__)


class Body:
    """身体：此刻的精力读数。"""

    def __init__(
        self,
        config: BodyConfig,
        monthly_token_limit: int | None,
        ledger: Ledger,
        clock: Clock,
        calendar: BudgetCalendar,
    ) -> None:
        """创建身体。账本要由调用方先打开。

        Args:
            config: 精力开关和档位分界。
            monthly_token_limit: 月度 token 上限（[safety] 的 monthly_token_limit），None 表示不限。
            ledger: 用量的来源。
            clock: 时钟，所有时间都从它取。
            calendar: 一天和一个月的边界，必须和账本用同一个。
        """
        self._config = config
        self._monthly_token_limit = monthly_token_limit
        self._ledger = ledger
        self._clock = clock
        self._calendar = calendar

    def read_energy(self) -> EnergyReading:
        """读此刻的精力。

        Raises:
            Exception: 账本读不了时原样抛出；调用方决定怎么办（网关当作不允许，
                显示的地方当作读不到）。
        """
        if not self._config.energy_enabled or self._monthly_token_limit is None:
            return UNLIMITED_READING
        now = self._clock.now()
        return compute_energy(
            monthly_limit=self._monthly_token_limit,
            used_this_month=self._ledger.tokens_used_in_month(now),
            used_today=self._ledger.tokens_used_in_day(now),
            days_left_in_month=self._calendar.days_left_in_month(now),
            config=self._config,
        )

    def refusal_reason(self, purpose: CallPurpose) -> str | None:
        """这次调用按精力该不该拒绝；该拒绝时返回原因，放行返回 None。

        回应用户的调用不看每日额度：用户开口就该有回应，花多了由月度上限兜底，
        后面每天的额度会自动变小。可选的调用（探索、闲聊）用到每日额度扣掉留给用户那份
        就停，推到明天。
        """
        if purpose is CallPurpose.USER_REPLY:
            return None
        reading = self.read_energy()
        if not reading.limited:
            return None
        assert reading.used_today is not None and reading.optional_ceiling is not None
        if reading.used_today >= reading.optional_ceiling:
            return (
                f"精力{reading.tier.value}：今天已用 {reading.used_today} token，"
                f"可选的调用最多用 {reading.optional_ceiling}"
                f"（今日额度 {reading.daily_quota}，其余留给回应用户），明天再说"
            )
        return None
