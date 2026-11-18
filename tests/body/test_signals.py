"""疲劳、压力、睡眠的纯计算和 Body 读数的测试：1.4 的验收（docs/08-roadmap.md）。"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from aureus.body import Body, compute_fatigue, compute_sleep, compute_stress
from aureus.common import BodyConfig, BudgetCalendar, ManualClock
from aureus.organs import CallAttempt
from aureus.safety import Ledger

START = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
CALENDAR = BudgetCalendar(UTC)
DEFAULTS = BodyConfig()


# ---------- 疲劳 ----------


def test_fatigue_scales_with_calls_and_caps_at_one() -> None:
    assert compute_fatigue(0, DEFAULTS).level == 0.0
    assert compute_fatigue(30, DEFAULTS).level == 0.5
    assert compute_fatigue(600, DEFAULTS).level == 1.0


def test_fatigue_is_high_exactly_at_the_threshold() -> None:
    just_below = compute_fatigue(41, DEFAULTS)  # 41/60 < 0.7
    at_threshold = compute_fatigue(42, DEFAULTS)  # 42/60 = 0.7
    assert (just_below.high, at_threshold.high) == (False, True)


def test_disabled_fatigue_reads_zero_and_never_high() -> None:
    reading = compute_fatigue(1000, BodyConfig(fatigue_enabled=False))
    assert (reading.enabled, reading.level, reading.high) == (False, 0.0, False)


# ---------- 压力 ----------


def test_one_failure_among_few_calls_is_not_full_stress() -> None:
    # 分母至少是窗口大小，第一次调用就失败不能直接算成 100%。
    reading = compute_stress([(False, "超时")], 0, DEFAULTS)
    assert reading.failure_rate == pytest.approx(1 / 20)
    assert not reading.high


def test_stress_takes_the_larger_of_failures_and_backlog() -> None:
    outcomes = [(False, "超时")] * 2 + [(True, None)] * 18  # 失败率 10%
    assert compute_stress(outcomes, 0, DEFAULTS).level == pytest.approx(0.1)
    assert compute_stress(outcomes, 3, DEFAULTS).level == pytest.approx(0.6)
    assert compute_stress(outcomes, 3, DEFAULTS).high
    assert compute_stress(outcomes, 99, DEFAULTS).level == 1.0


def test_stress_only_looks_at_the_latest_window() -> None:
    # 新的在前：最近 20 次全成功，更早的失败不再算。
    outcomes = [(True, None)] * 20 + [(False, "旧的失败")] * 30
    reading = compute_stress(outcomes, 0, DEFAULTS)
    assert (reading.failed_calls, reading.sampled_calls, reading.level) == (0, 20, 0.0)


def test_failure_reasons_are_distinct_newest_first_and_capped() -> None:
    outcomes = [(False, "超时"), (False, "超时"), (False, None), (False, "限流"), (False, "断网")]
    reading = compute_stress(outcomes, 0, DEFAULTS)
    assert reading.recent_failure_reasons == ("超时", "原因没有记录", "限流")


def test_negative_backlog_counts_as_zero() -> None:
    assert compute_stress([], -4, DEFAULTS).level == 0.0


def test_disabled_stress_ignores_failures_and_backlog() -> None:
    reading = compute_stress([(False, "超时")] * 20, 9, BodyConfig(stress_enabled=False))
    assert (reading.enabled, reading.level, reading.high) == (False, 0.0, False)


# ---------- 睡眠 ----------


@pytest.mark.parametrize(
    ("hour", "night"),
    [(0, False), (1, True), (6, True), (7, False), (12, False)],
)
def test_default_night_window(hour: int, night: bool) -> None:
    assert compute_sleep(hour, DEFAULTS).night is night


@pytest.mark.parametrize(
    ("hour", "night"),
    [(22, False), (23, True), (0, True), (6, True), (7, False)],
)
def test_night_window_can_cross_midnight(hour: int, night: bool) -> None:
    config = BodyConfig(night_start_hour=23, night_end_hour=7)
    assert compute_sleep(hour, config).night is night


def test_disabled_sleep_is_never_night() -> None:
    reading = compute_sleep(3, BodyConfig(sleep_enabled=False))
    assert (reading.enabled, reading.night) == (False, False)


def test_night_window_cannot_be_empty() -> None:
    with pytest.raises(ValidationError, match="夜间时段"):
        BodyConfig(night_start_hour=4, night_end_hour=4)


# ---------- Body 从账本和时钟读 ----------


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def ledger(tmp_path: Path, clock: ManualClock) -> Iterator[Ledger]:
    opened = Ledger(tmp_path / "aureus.db", clock, calendar=CALENDAR)
    opened.open()
    yield opened
    opened.close()


def make_body(ledger: Ledger, clock: ManualClock, **config: object) -> Body:
    return Body(BodyConfig(**config), None, ledger, clock, CALENDAR)  # pyright: ignore[reportArgumentType]


def record_call(ledger: Ledger, *, succeeded: bool = True, reason: str | None = None) -> None:
    attempt = CallAttempt(
        model="m", provider="p", prompt_tokens=10, completion_tokens=5,
        succeeded=succeeded, failure_reason=reason,
    )  # fmt: skip
    ledger.record("replyer", [attempt])


def test_fatigue_counts_only_calls_inside_the_window(ledger: Ledger, clock: ManualClock) -> None:
    for _ in range(3):
        record_call(ledger)  # 12:00
    clock.advance(timedelta(minutes=20))
    record_call(ledger)  # 12:20
    body = make_body(ledger, clock)

    clock.advance(timedelta(minutes=15))  # 12:35：窗口是 12:05 之后
    assert body.read_fatigue().calls_in_window == 1
    clock.advance(timedelta(minutes=30))  # 13:05：全都出窗口了
    assert body.read_fatigue().calls_in_window == 0


def test_stress_reads_recent_outcomes_from_the_ledger(ledger: Ledger, clock: ManualClock) -> None:
    for _ in range(10):
        record_call(ledger)
    for _ in range(4):
        record_call(ledger, succeeded=False, reason="模型服务 503")
    body = make_body(ledger, clock)

    reading = body.read_stress(backlog=0)

    assert (reading.sampled_calls, reading.failed_calls) == (14, 4)
    assert reading.failure_rate == pytest.approx(4 / 20)
    assert reading.recent_failure_reasons == ("模型服务 503",)


def test_stress_includes_the_backlog_passed_by_the_heartbeat(
    ledger: Ledger, clock: ManualClock
) -> None:
    body = make_body(ledger, clock)
    assert body.read_stress(backlog=5).level == 1.0


def test_night_uses_the_calendar_time_zone(ledger: Ledger) -> None:
    # UTC 20:00 在上海是次日凌晨 4 点（夜间），在 UTC 是晚上 8 点（不是夜间）。
    clock = ManualClock(datetime(2026, 11, 10, 20, 0, tzinfo=UTC))
    shanghai = Body(BodyConfig(), None, ledger, clock, BudgetCalendar(ZoneInfo("Asia/Shanghai")))
    utc = Body(BodyConfig(), None, ledger, clock, CALENDAR)
    assert (shanghai.read_sleep().night, utc.read_sleep().night) == (True, False)


def test_disabled_signals_do_not_touch_the_ledger(ledger: Ledger, clock: ManualClock) -> None:
    body = make_body(ledger, clock, fatigue_enabled=False, stress_enabled=False)
    ledger.close()  # 账本关了：读它会抛异常

    reading = body.read(backlog=2)

    assert not reading.fatigue.enabled
    assert not reading.stress.enabled


def test_read_bundles_all_four_signals(ledger: Ledger, clock: ManualClock) -> None:
    record_call(ledger)
    reading = make_body(ledger, clock).read(backlog=1)
    assert reading.fatigue.calls_in_window == 1
    assert reading.stress.backlog == 1
    assert reading.energy.tier.value == "不限"
    assert reading.sleep.night is False
