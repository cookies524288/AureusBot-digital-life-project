"""精力计算的测试：1.3 的纯计算部分（04 号文档第 4 节，D53、D56）。

- 每日额度 = （月上限 - 今天之前已用）÷ 本月剩余天数，前几天省下的摊给后面；
- 档位按精力比例分，分界上的值归到更好的一档；
- 数据异常（今天比本月用得多、天数是 0）朝安全方向倒。
"""

import pytest

from aureus.body import EnergyTier, classify, compute_energy
from aureus.common import BodyConfig

CONFIG = BodyConfig()
LIMIT = 3_000_000


def energy(used_this_month: int = 0, used_today: int = 0, days: int = 30, limit: int = LIMIT):
    return compute_energy(
        monthly_limit=limit,
        used_this_month=used_this_month,
        used_today=used_today,
        days_left_in_month=days,
        config=CONFIG,
    )


# ---------- 每日额度 ----------


def test_fresh_month_splits_the_limit_evenly() -> None:
    reading = energy()
    assert reading.daily_quota == 100_000
    assert reading.fraction == 1.0
    assert reading.tier is EnergyTier.NORMAL
    assert reading.limited


def test_unused_budget_is_spread_over_the_remaining_days() -> None:
    # 前 10 天一个都没用：第 11 天开始，剩下 20 天平分整个月的额度。
    reading = energy(days=20)
    assert reading.daily_quota == 150_000


def test_overspending_early_shrinks_the_later_quotas() -> None:
    # 前 10 天用了 1,500,000（是该用的 1.5 倍），第 11 天起每天只剩 75,000。
    reading = energy(used_this_month=1_500_000, used_today=0, days=20)
    assert reading.daily_quota == 75_000


def test_today_is_counted_against_todays_quota_not_the_earlier_days() -> None:
    reading = energy(used_this_month=40_000, used_today=40_000)
    assert reading.daily_quota == 100_000  # 额度是今天之前的账算的，今天花了也不变
    assert reading.used_today == 40_000
    assert reading.remaining_today == 60_000
    assert reading.fraction == pytest.approx(0.6)


def test_quota_rounds_down_so_the_month_never_overspends() -> None:
    reading = energy(limit=100, days=30)
    assert reading.daily_quota == 3  # 100 / 30 = 3.33，向下取整
    assert reading.optional_ceiling == 2  # 3 * 0.85 = 2.55，向下取整


def test_optional_ceiling_keeps_the_user_reserve() -> None:
    reading = energy()
    assert reading.optional_ceiling == 85_000  # 15% 留给用户


# ---------- 档位 ----------


@pytest.mark.parametrize(
    ("fraction", "tier"),
    [
        (1.0, EnergyTier.NORMAL),
        (0.5, EnergyTier.NORMAL),  # 分界上的值归到更好的一档
        (0.4999, EnergyTier.SAVING),
        (0.2, EnergyTier.SAVING),
        (0.1999, EnergyTier.LOW),
        (0.05, EnergyTier.LOW),
        (0.0499, EnergyTier.OVERDRAFT),
        (0.0, EnergyTier.OVERDRAFT),
    ],
)
def test_tier_boundaries(fraction: float, tier: EnergyTier) -> None:
    assert classify(fraction, CONFIG) is tier


def test_tier_thresholds_are_configurable() -> None:
    config = BodyConfig(saving_below=0.8, low_below=0.4, overdraft_below=0.1)
    assert classify(0.7, config) is EnergyTier.SAVING
    assert classify(0.3, config) is EnergyTier.LOW
    assert classify(0.05, config) is EnergyTier.OVERDRAFT


def test_spending_through_the_day_walks_down_the_tiers() -> None:
    tiers = [energy(used, used).tier for used in (0, 50_000, 80_000, 90_000, 100_000)]
    assert tiers == [
        EnergyTier.NORMAL,
        EnergyTier.NORMAL,
        EnergyTier.SAVING,
        EnergyTier.LOW,
        EnergyTier.OVERDRAFT,
    ]


# ---------- 数据异常时朝安全方向倒 ----------


def test_going_over_todays_quota_is_overdraft_with_zero_remaining() -> None:
    reading = energy(used_this_month=130_000, used_today=130_000)
    assert reading.remaining_today == 0
    assert reading.fraction == 0.0
    assert reading.tier is EnergyTier.OVERDRAFT


def test_exhausted_month_has_no_quota() -> None:
    reading = energy(used_this_month=LIMIT + 500, used_today=500, days=10)
    assert reading.daily_quota == (LIMIT - (LIMIT)) // 10 == 0
    assert reading.month_remaining == 0
    assert reading.fraction == 0.0
    assert reading.tier is EnergyTier.OVERDRAFT


def test_today_larger_than_month_is_clamped_to_the_month() -> None:
    # 账本不会这样，但算法不该因为脏数据给出负的“之前已用”而多给额度。
    reading = energy(used_this_month=1_000, used_today=9_999_999)
    assert reading.used_today == 1_000
    assert reading.daily_quota == LIMIT // 30


def test_negative_usage_is_treated_as_zero_today() -> None:
    reading = energy(used_this_month=0, used_today=-5)
    assert reading.used_today == 0


def test_zero_or_negative_days_left_counts_as_one() -> None:
    assert energy(days=0).daily_quota == LIMIT
    assert energy(days=-3).daily_quota == LIMIT
