"""生物模型随时间变化的测试：0.9a 的验收标准（docs/08-roadmap.md，D42）。

- 同一段时间用不同的心跳间隔推进，结果一致；
- 连续运行几天不会卡在极端值，精力会恢复；
- 极端的时间间隔和不合法的输入不会弄坏状态；
- 社交需求和新奇感随时间上涨，谈话后回落；日周期按本地时间算。
"""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import cache
from typing import Any

import pytest

from aureus.common import BiomodelConfig
from aureus.common.clock import ManualClock
from aureus.core.biomodel import BiologicalState, HormoneState

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
        "energy": bio.energy,
        "dopamine": bio.hormones.dopamine,
        "cortisol": bio.hormones.cortisol,
        "serotonin": bio.hormones.serotonin,
        "noradrenaline": bio.hormones.noradrenaline,
        "social": bio.social_deficit,
        "novelty": bio.novelty_craving,
    }


def assert_in_range(bio: BiologicalState) -> None:
    for name, value in snapshot(bio).items():
        assert math.isfinite(value) and 0.0 <= value <= 1.0, f"{name}={value}"


def test_documented_default_parameters() -> None:
    # D43 定下的默认值；改了要同步改 D43 和 config/aureus.example.toml。
    _, bio = make()
    assert bio.energy_recovery_rate == 0.15  # 每小时
    assert bio.social_timeout_per_hour == 0.03
    assert bio.novelty_growth_per_hour == 0.05
    assert bio.social_relief_per_conversation == 0.05  # 每次回复
    assert bio.novelty_relief_per_conversation == 0.03


def test_defaults_come_from_the_config_section() -> None:
    _, bio = make()
    for name, value in BiomodelConfig().model_dump().items():
        assert getattr(bio, name) == value, name


def test_from_config_uses_every_tunable_parameter() -> None:
    clock = ManualClock(datetime(2026, 11, 17, tzinfo=UTC))
    config = BiomodelConfig(
        energy_recovery_rate=0.3,
        social_timeout_per_hour=0.1,
        novelty_growth_per_hour=0.2,
        social_relief_per_conversation=0.4,
        novelty_relief_per_conversation=0.5,
    )
    bio = BiologicalState.from_config(config, clock, CHINA)
    assert bio.clock is clock
    assert bio.timezone is CHINA
    for name, value in config.model_dump().items():
        assert getattr(bio, name) == value, name


# ---------- 结果不随心跳快慢变化（D42 第 1、2 条） ----------


def perturbed_run(hour: int, step_seconds: float) -> dict[str, float]:
    # 起点故意偏离基线，这样每个量都在变化，差异才测得出来。
    hormones = HormoneState(dopamine=0.9, cortisol=0.8, serotonin=0.2, noradrenaline=0.7)
    clock, bio = make(hour, hormones=hormones, energy=0.3, social_deficit=0.2)
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


# ---------- 精力会恢复，不会卡在极端值（D42 第 4 条） ----------


def test_energy_recovers_from_exhaustion() -> None:
    clock, bio = make(energy=0.05)
    run(clock, bio, 24 * HOUR, 60)
    assert 0.5 < bio.energy < 0.65


def test_energy_relaxes_down_from_full() -> None:
    clock, bio = make(energy=1.0)
    run(clock, bio, 24 * HOUR, 60)
    assert 0.5 < bio.energy < 0.65


def test_idle_energy_settles_instead_of_draining() -> None:
    # 原来空闲时每秒耗 0.002，约 4 分钟耗光，之后一直“濒死”（D41 第 2 条）。
    clock, bio = make()
    seen: list[float] = []
    run(clock, bio, 72 * HOUR, 60, on_step=lambda: seen.append(bio.energy))
    assert min(seen) > 0.4
    assert max(seen) < 0.7


def test_a_month_of_hourly_ticks_stays_in_range() -> None:
    clock, bio = make()
    run(clock, bio, 30 * 24 * HOUR, HOUR, on_step=lambda: assert_in_range(bio))
    assert 0.5 < bio.energy < 0.65


# ---------- 很长的间隔、不合法的输入 ----------


@pytest.mark.parametrize("days", [1, 7, 365])
def test_long_gap_in_one_update_stays_finite_and_in_range(days: int) -> None:
    # 电脑休眠一年再醒来，一次 update 也不能花很久，结果也不能是乱的（最多切 2000 段）。
    clock, bio = make()
    advance(clock, bio, days * 24 * HOUR)
    assert_in_range(bio)
    assert 0.5 < bio.energy < 0.65


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


# ---------- 社交需求、新奇感随时间上涨，谈话后回落（D42 第 5–7 条） ----------


def test_social_need_rises_by_the_hour() -> None:
    clock, bio = make(social_deficit=0.3)
    advance(clock, bio, 10 * HOUR)
    assert bio.social_deficit == pytest.approx(0.3 + 10 * 0.03)


def test_social_need_stops_at_one() -> None:
    clock, bio = make(social_deficit=0.95)
    advance(clock, bio, 10 * HOUR)
    assert bio.social_deficit == 1.0


def test_novelty_craving_rises_by_the_hour() -> None:
    # 原来每秒涨 0.0005，约 20 分钟涨满（D42 第 6 条）。
    clock, bio = make(novelty_craving=0.4)
    advance(clock, bio, 20 * 60)
    assert bio.novelty_craving < 0.42
    advance(clock, bio, 12 * HOUR)  # 0.4 + 12 × 0.05 = 1.0，再多也停在 1
    assert bio.novelty_craving == 1.0


def test_conversation_relieves_social_need_and_novelty_craving() -> None:
    _, bio = make(social_deficit=0.5, novelty_craving=0.5)
    hormones_before = snapshot(bio)
    bio.on_conversation()
    assert bio.social_deficit == pytest.approx(0.45)
    assert bio.novelty_craving == pytest.approx(0.47)
    # 激素和精力不动：由真实事件触发激素是 Phase 2 的事（D20）。
    after = snapshot(bio)
    for name in ("energy", "dopamine", "cortisol", "serotonin", "noradrenaline"):
        assert after[name] == hormones_before[name], name


def test_conversation_relief_stops_at_zero() -> None:
    _, bio = make(social_deficit=0.02, novelty_craving=0.01)
    bio.on_conversation()
    assert bio.social_deficit == 0.0
    assert bio.novelty_craving == 0.0


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
