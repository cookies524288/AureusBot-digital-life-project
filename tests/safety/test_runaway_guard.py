"""失控拦截的测试：频率限制、连续失败暂停、半开、按器官分开（D39，06 号文档 12.5 节）。

时间全部用手动时钟拨动，不真的等。
"""

from datetime import UTC, datetime, timedelta

import pytest

from aureus.common import ManualClock, SafetyConfig
from aureus.safety import RunawayGuard

START = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
ORGAN = "replyer"


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


def make_guard(clock: ManualClock, **overrides: int | bool) -> RunawayGuard:
    return RunawayGuard(SafetyConfig(**overrides), clock)  # pyright: ignore[reportArgumentType]


def call(guard: RunawayGuard, organ_id: str = ORGAN) -> None:
    """模拟网关放行一次调用：先检查，必须通过，再记一次。"""
    assert guard.blocked_reason(organ_id) is None
    guard.admit(organ_id)


# ---------- 频率限制 ----------


def test_calls_below_the_rate_limit_are_allowed(clock: ManualClock) -> None:
    guard = make_guard(clock, max_calls_per_minute=3)
    for _ in range(3):
        call(guard)


def test_call_beyond_the_rate_limit_is_blocked(clock: ManualClock) -> None:
    guard = make_guard(clock, max_calls_per_minute=3)
    for _ in range(3):
        call(guard)
    reason = guard.blocked_reason(ORGAN)
    assert reason is not None
    assert "3" in reason


def test_checking_does_not_use_up_the_rate_limit(clock: ManualClock) -> None:
    guard = make_guard(clock, max_calls_per_minute=2)
    for _ in range(10):
        assert guard.blocked_reason(ORGAN) is None  # 只检查、不放行，不该占名额
    call(guard)
    call(guard)


def test_window_slides_with_the_clock(clock: ManualClock) -> None:
    guard = make_guard(clock, max_calls_per_minute=2)
    call(guard)  # 第 0 秒
    clock.advance(timedelta(seconds=30))
    call(guard)  # 第 30 秒
    clock.advance(timedelta(seconds=29))  # 第 59 秒：两次都还在窗口里
    assert guard.blocked_reason(ORGAN) is not None
    clock.advance(timedelta(seconds=1))  # 第 60 秒：第一次刚好滑出去
    assert guard.blocked_reason(ORGAN) is None
    call(guard)
    assert guard.blocked_reason(ORGAN) is not None  # 第 30 秒那次和刚才这次，又满了


def test_rate_limit_is_per_organ(clock: ManualClock) -> None:
    guard = make_guard(clock, max_calls_per_minute=1)
    call(guard, "replyer")
    assert guard.blocked_reason("replyer") is not None
    assert guard.blocked_reason("planner") is None


# ---------- 连续失败暂停 ----------


def fail_times(guard: RunawayGuard, times: int, organ_id: str = ORGAN) -> None:
    for _ in range(times):
        call(guard, organ_id)
        guard.report(organ_id, succeeded=False)


def test_failures_below_the_threshold_do_not_pause(clock: ManualClock) -> None:
    guard = make_guard(clock, max_consecutive_failures=3, max_calls_per_minute=100)
    fail_times(guard, 2)
    assert guard.blocked_reason(ORGAN) is None


def test_consecutive_failures_pause_the_organ(clock: ManualClock) -> None:
    guard = make_guard(
        clock, max_consecutive_failures=3, failure_pause_seconds=300, max_calls_per_minute=100
    )
    fail_times(guard, 3)
    reason = guard.blocked_reason(ORGAN)
    assert reason is not None
    assert "300" in reason  # 告诉调用方还要等多久


def test_pause_ends_after_the_configured_time(clock: ManualClock) -> None:
    guard = make_guard(
        clock, max_consecutive_failures=2, failure_pause_seconds=300, max_calls_per_minute=100
    )
    fail_times(guard, 2)
    clock.advance(timedelta(seconds=299))
    assert guard.blocked_reason(ORGAN) is not None
    clock.advance(timedelta(seconds=1))
    assert guard.blocked_reason(ORGAN) is None


def test_one_more_failure_after_the_pause_pauses_again(clock: ManualClock) -> None:
    # 熔断器的「半开」：暂停结束后只放一次试探，再失败就再暂停，不用重新攒够失败次数。
    guard = make_guard(
        clock, max_consecutive_failures=3, failure_pause_seconds=60, max_calls_per_minute=100
    )
    fail_times(guard, 3)
    clock.advance(timedelta(seconds=60))
    fail_times(guard, 1)
    assert guard.blocked_reason(ORGAN) is not None


def test_success_after_the_pause_closes_the_breaker(clock: ManualClock) -> None:
    guard = make_guard(
        clock, max_consecutive_failures=3, failure_pause_seconds=60, max_calls_per_minute=100
    )
    fail_times(guard, 3)
    clock.advance(timedelta(seconds=60))
    call(guard)
    guard.report(ORGAN, succeeded=True)
    fail_times(guard, 2)  # 失败数已清零，2 次不够再暂停
    assert guard.blocked_reason(ORGAN) is None
    fail_times(guard, 1)
    assert guard.blocked_reason(ORGAN) is not None


def test_a_success_in_between_resets_the_count(clock: ManualClock) -> None:
    guard = make_guard(clock, max_consecutive_failures=3, max_calls_per_minute=100)
    fail_times(guard, 2)
    call(guard)
    guard.report(ORGAN, succeeded=True)
    fail_times(guard, 2)
    assert guard.blocked_reason(ORGAN) is None


def test_failures_are_counted_per_organ(clock: ManualClock) -> None:
    guard = make_guard(clock, max_consecutive_failures=2, max_calls_per_minute=100)
    fail_times(guard, 1, "replyer")
    fail_times(guard, 1, "planner")
    assert guard.blocked_reason("replyer") is None
    assert guard.blocked_reason("planner") is None
    fail_times(guard, 1, "replyer")
    assert guard.blocked_reason("replyer") is not None
    assert guard.blocked_reason("planner") is None


# ---------- 开关 ----------


def test_disabled_guard_never_blocks(clock: ManualClock) -> None:
    guard = make_guard(
        clock, runaway_guard_enabled=False, max_calls_per_minute=1, max_consecutive_failures=1
    )
    assert guard.enabled is False
    for _ in range(5):
        call(guard)
        guard.report(ORGAN, succeeded=False)
    assert guard.blocked_reason(ORGAN) is None


def test_guard_is_enabled_by_default(clock: ManualClock) -> None:
    assert make_guard(clock).enabled is True
