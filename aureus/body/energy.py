"""精力：把「今日还剩多少额度」算成档位（04 号文档第 4 节，D53、D56）。

精力是纯计算：输入是月度上限、本月和今天已经用掉的 token、本月还剩几天，
输出是一份读数。这里不读账本、不读时间，调用方（Body）取好数字传进来。
"""

from dataclasses import dataclass
from enum import StrEnum

from aureus.common import BodyConfig


class EnergyTier(StrEnum):
    """精力档位。值是显示用的中文名，也是写进时间线的文字。"""

    UNLIMITED = "不限"
    NORMAL = "正常"
    SAVING = "节省"
    LOW = "低电量"
    OVERDRAFT = "透支保护"


@dataclass(frozen=True)
class EnergyReading:
    """某一刻的精力读数。

    不限（没设月度上限，或精力信号关着）时，数字字段都是 None，档位是 UNLIMITED：
    调用方把它当成「正常档」对待，但说话时要如实说「没设上限」，不能说成有精力。
    """

    tier: EnergyTier
    # 今日剩余额度 ÷ 今日额度，[0, 1]；透支（今天已经用超）时是 0。
    fraction: float | None = None
    daily_quota: int | None = None
    used_today: int | None = None
    # 今天还能用多少 token 才到额度，透支时是 0。
    remaining_today: int | None = None
    month_remaining: int | None = None
    days_left_in_month: int | None = None
    # 可选的调用最多能用到今天额度的多少（已经扣掉留给用户的部分）；None 表示不限。
    optional_ceiling: int | None = None

    @property
    def limited(self) -> bool:
        """是否真的按额度在算（False 就是不限）。"""
        return self.fraction is not None


UNLIMITED_READING = EnergyReading(tier=EnergyTier.UNLIMITED)


def classify(fraction: float, config: BodyConfig) -> EnergyTier:
    """按精力比例定档位。分界上的值归到更好的那一档（刚好 50% 还是正常档）。"""
    if fraction < config.overdraft_below:
        return EnergyTier.OVERDRAFT
    if fraction < config.low_below:
        return EnergyTier.LOW
    if fraction < config.saving_below:
        return EnergyTier.SAVING
    return EnergyTier.NORMAL


def compute_energy(
    *,
    monthly_limit: int,
    used_this_month: int,
    used_today: int,
    days_left_in_month: int,
    config: BodyConfig,
) -> EnergyReading:
    """算今天的精力。

    每日额度 = （月上限 - 今天之前已用）÷ 本月剩余天数（含今天）。前几天省下的会摊给后面，
    前几天花多了，后面每天的额度就小（04 号文档第 4 节）。

    Args:
        monthly_limit: 月度 token 上限，正整数。
        used_this_month: 本月（含今天）已用 token。
        used_today: 今天已用 token，不会比本月已用更大（算出来更大时按本月已用算）。
        days_left_in_month: 本月还剩几天，含今天，至少 1。
        config: 档位分界和留给用户的比例。

    Returns:
        精力读数。本月已经用完时额度是 0，精力 0，透支保护。
    """
    days = max(1, days_left_in_month)
    used_today = max(0, min(used_today, used_this_month))
    used_before_today = used_this_month - used_today
    month_remaining = max(0, monthly_limit - used_this_month)
    # 向下取整：宁可少给一点，月底也不会因为取整多花出去。
    quota = max(0, monthly_limit - used_before_today) // days
    remaining = max(0, quota - used_today)
    fraction = remaining / quota if quota > 0 else 0.0
    return EnergyReading(
        tier=classify(fraction, config),
        fraction=fraction,
        daily_quota=quota,
        used_today=used_today,
        remaining_today=remaining,
        month_remaining=month_remaining,
        days_left_in_month=days,
        optional_ceiling=int(quota * (1.0 - config.user_reserve_fraction)),
    )
