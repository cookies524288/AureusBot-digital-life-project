"""主动联系的十一道把关、真实原因和退避测试（D76）。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aureus.body import EnergyTier, SocialNeedReading
from aureus.common import InitiativeConfig
from aureus.heartbeat.initiative import InitiativeState, daily_limit, decide, next_interval

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
CONFIG = InitiativeConfig(enabled=True)
HIGH = SocialNeedReading(enabled=True, level=0.81, hours_since=20.0, high=True)


def arguments(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "now": NOW,
        "config": CONFIG,
        "state": InitiativeState(),
        "social_need": HIGH,
        "due_items": (),
        "effective_tier": EnergyTier.NORMAL,
        "night": False,
        "halted": False,
        "busy": False,
        "cautious": False,
        "has_channel": True,
    }
    return values | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"config": InitiativeConfig()},
        {"halted": True},
        {"busy": True},
        {"night": True},
        {"effective_tier": EnergyTier.SAVING},
        {"has_channel": False},
        {"state": InitiativeState(unanswered_streak=3)},
        {"state": InitiativeState(pending_since=NOW)},
        {"state": InitiativeState(contacts_today=2)},
        {"state": InitiativeState(last_considered_at=NOW - timedelta(hours=2))},
        {"social_need": None},
    ],
)
def test_each_of_the_eleven_gates_blocks(changes: dict[str, Any]) -> None:
    decision = decide(**arguments(**changes))
    assert not decision.allowed
    assert decision.blocked_by
    assert not decision.reasons


def test_first_failed_gate_is_reported() -> None:
    decision = decide(**arguments(config=InitiativeConfig(), halted=True, busy=True))
    assert decision.blocked_by == "主动联系已关闭"


@pytest.mark.parametrize("tier", [None, EnergyTier.NORMAL, EnergyTier.UNLIMITED])
def test_high_social_need_has_a_real_reason(tier: EnergyTier | None) -> None:
    decision = decide(**arguments(effective_tier=tier))
    assert decision.allowed and not decision.blocked_by
    assert decision.reasons == ("距上次和用户好好聊过 20.0 小时，社交需求 81%",)


def test_due_commitment_alone_is_enough() -> None:
    decision = decide(**arguments(social_need=None, due_items=["#3 提醒他开会"]))
    assert decision.allowed
    assert decision.reasons == ("答应的事到期了：#3 提醒他开会",)


def test_all_reasons_are_kept_in_order() -> None:
    decision = decide(
        **arguments(social_need=replace(HIGH, hours_since=None), due_items=["#3 开会"])
    )
    assert decision.reasons == ("还没和用户好好聊过，社交需求 81%", "答应的事到期了：#3 开会")


def test_disabled_or_low_social_need_is_not_a_reason() -> None:
    for reading in (replace(HIGH, enabled=False), replace(HIGH, high=False)):
        assert not decide(**arguments(social_need=reading)).allowed


@pytest.mark.parametrize(("streak", "hours"), [(0, 3), (1, 6), (2, 12), (1000000, 48)])
def test_intervals_double_and_cap(streak: int, hours: int) -> None:
    assert next_interval(streak, CONFIG) == timedelta(hours=hours)


def test_interval_boundary_and_clock_rollback() -> None:
    assert decide(
        **arguments(state=InitiativeState(last_considered_at=NOW - timedelta(hours=3)))
    ).allowed
    assert not decide(
        **arguments(state=InitiativeState(last_considered_at=NOW + timedelta(hours=1)))
    ).allowed


@pytest.mark.parametrize(("maximum", "expected"), [(2, 1), (1, 1), (5, 2)])
def test_caution_halves_daily_limit(maximum: int, expected: int) -> None:
    config = InitiativeConfig(max_per_day=maximum)
    assert daily_limit(config, cautious=True) == expected
    assert daily_limit(config, cautious=False) == maximum
