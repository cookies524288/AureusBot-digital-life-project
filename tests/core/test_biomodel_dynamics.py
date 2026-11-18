"""生物模型随时间变化的测试：0.9a 的验收标准（docs/08-roadmap.md，D42）。

- 同一段时间用不同的心跳间隔推进，结果一致；
- 连续运行几天不会卡在极端值，激素会回到基线；
- 极端的时间间隔和不合法的输入不会弄坏状态；
- 日周期按本地时间算。

精力、社交需求、新奇感已经不在这里（D53、D56）：精力见 tests/body/。
"""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import cache
from typing import Any

import pytest

from aureus.common.clock import ManualClock
from aureus.core.biomodel import (
    SEROTONIN_BASELINE,
    BiologicalState,
    HormoneState,
)

HOUR = 3600.0
CHINA = timezone(timedelta(hours=8))
# D42 第 1 条：不同心跳间隔算出的结果，最多差千分之一。
TOLERANCE = 1e-3


def make(hour: int = 0, zone: tzinfo = UTC, **fields: Any) -> tuple[ManualClock, BiologicalState]:
    clock = ManualClock(datetime(2026, 11, 17, hour, tzinfo=UTC))
    return clock, BiologicalState(clock=clock, timezone=zone, **fields)


def advance(clock: ManualClock, bio: BiologicalState, seconds: float) -> None:
    clock.advance(timedelta(seconds=seconds))
    bio.update(seconds)


def run(
    clock: ManualClock,
    bio: BiologicalState,
    total_seconds: float,
    step_seconds: float,
    on_step: Callable[[], None] | None = None,
) -> None:
    for _ in range(round(total_seconds / step_seconds)):
        advance(clock, bio, step_seconds)
        if on_step is not None:
            on_step()


def snapshot(bio: BiologicalState) -> dict[str, float]:
    return {
        "dopamine": bio.hormones.dopamine,
        "cortisol": bio.hormones.cortisol,
        "serotonin": bio.hormones.serotonin,
        "noradrenaline": bio.hormones.noradrenaline,
    }


def assert_in_range(bio: BiologicalState) -> None:
    for name, value in snapshot(bio).items():
        assert math.isfinite(value) and 0.0 <= value <= 1.0, f"{name}={value}"


# ---------- 结果不随心跳快慢变化（D42 第 1、2 条） ----------


def perturbed_run(hour: int, step_seconds: float) -> dict[str, float]:
    # 起点故意偏离基线，这样每个量都在变化，差异才测得出来。
    hormones = HormoneState(dopamine=0.9, cortisol=0.8, serotonin=0.2, noradrenaline=0.7)
    clock, bio = make(hour, hormones=hormones)
    run(clock, bio, 6 * HOUR, step_seconds)
    return snapshot(bio)


@cache
def reference(hour: int) -> dict[str, float]:
    return perturbed_run(hour, 1.0)  # 每秒一拍，当作最接近真值的参照


@pytest.mark.parametrize("hour", [3, 9, 15, 22])
@pytest.mark.parametrize("step_seconds", [5.0, 60.0, HOUR])
def test_result_does_not_depend_on_tick_length(hour: int, step_seconds: float) -> None:
    actual = perturbed_run(hour, step_seconds)
    for name, expected in reference(hour).items():
        assert actual[name] == pytest.approx(expected, abs=TOLERANCE), name


# ---------- 激素会回到基线，不会卡在极端值（D42 第 4 条） ----------


def test_hormones_return_to_baseline_after_a_spike() -> None:
    hormones = HormoneState(cortisol=1.0, serotonin=0.0)
    clock, bio = make(hour=8, hormones=hormones)
    run(clock, bio, 7 * 24 * HOUR, 60)
    # 皮质醇半衰期 1 小时：一周后只剩日周期的起伏，不会停在 1；
    # 血清素半衰期 24 小时：一周后回到基线附近。
    assert 0.2 < bio.hormones.cortisol < 0.8
    assert bio.hormones.serotonin == pytest.approx(SEROTONIN_BASELINE, abs=0.02)


def test_idle_cortisol_repeats_the_same_daily_cycle() -> None:
    # 没有任何事件时，皮质醇只跟着日周期走：第二天和第三天的最高、最低值一样，没有漂向极端。
    clock, bio = make(hour=8)
    run(clock, bio, 24 * HOUR, 60)
    peaks_and_troughs = []
    for _ in range(2):
        seen: list[float] = []

        def watch(seen: list[float] = seen) -> None:
            seen.append(bio.hormones.cortisol)

        run(clock, bio, 24 * HOUR, 60, on_step=watch)
        peaks_and_troughs.append((max(seen), min(seen)))
    assert peaks_and_troughs[0] == pytest.approx(peaks_and_troughs[1], abs=0.01)


def test_a_month_of_hourly_ticks_stays_in_range() -> None:
    clock, bio = make()
    run(clock, bio, 30 * 24 * HOUR, HOUR, on_step=lambda: assert_in_range(bio))


# ---------- 很长的间隔、不合法的输入 ----------


@pytest.mark.parametrize("days", [1, 7, 365])
def test_long_gap_in_one_update_stays_finite_and_in_range(days: int) -> None:
    # 电脑休眠一年再醒来，一次 update 也不能花很久，结果也不能是乱的（最多切 2000 段）。
    clock, bio = make()
    advance(clock, bio, days * 24 * HOUR)
    assert_in_range(bio)


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_invalid_dt_is_rejected_without_changing_anything(bad: float) -> None:
    _, bio = make()
    before = snapshot(bio)
    with pytest.raises(ValueError, match="dt"):
        bio.update(bad)
    assert snapshot(bio) == before


def test_zero_dt_changes_nothing() -> None:
    _, bio = make()
    before = snapshot(bio)
    bio.update(0.0)
    assert snapshot(bio) == before


# ---------- 日周期按本地时间算（D42 第 3 条） ----------


def hour_of_cortisol_peak(zone: tzinfo) -> int:
    """跑两天，返回第二天里皮质醇最高的那个本地钟点。"""
    clock, bio = make(zone=zone)
    run(clock, bio, 24 * HOUR, 60)
    by_hour: dict[int, float] = {}

    def record() -> None:
        moment = clock.now().astimezone(zone)
        if moment.minute == 0:
            by_hour[moment.hour] = bio.hormones.cortisol

    run(clock, bio, 24 * HOUR, 60, on_step=record)
    return max(by_hour, key=lambda hour: by_hour[hour])


@pytest.mark.parametrize("zone", [UTC, CHINA])
def test_cortisol_peaks_in_the_local_morning(zone: tzinfo) -> None:
    # 日周期曲线的峰值在早上 8 点；半衰期 1 小时，实际峰值稍晚一点。
    assert 8 <= hour_of_cortisol_peak(zone) <= 10


def test_circadian_phase_follows_the_local_clock_and_counts_seconds() -> None:
    clock, bio = make(hour=0, zone=CHINA)  # UTC 0 点 = 东八区早上 8 点
    advance(clock, bio, 30)
    assert bio.circadian_phase == pytest.approx((8 * HOUR + 30) / (24 * HOUR))
