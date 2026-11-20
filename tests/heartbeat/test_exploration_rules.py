"""小探索十道把关、兴趣轮换及总结解析（D79）。"""

import json
from datetime import timedelta
from typing import Any

import pytest

from aureus.body import EnergyTier
from aureus.common import ExplorationConfig
from aureus.heartbeat.exploration import (
    ExplorationState,
    InterestStatus,
    daily_limit,
    decide,
    parse_summary,
)
from tests.agenda.test_agenda_store import NOW


def allowed(**changes: Any):
    args: dict[str, Any] = dict(
        now=NOW,
        config=ExplorationConfig(enabled=True),
        state=ExplorationState(),
        interests=[InterestStatus(1)],
        effective_tier=EnergyTier.NORMAL,
        night=False,
        halted=False,
        busy=False,
        cautious=False,
        has_search=True,
        has_organ=True,
    )
    args.update(changes)
    return decide(**args)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"config": ExplorationConfig()}, "disabled"),
        ({"has_search": False}, "no_search"),
        ({"has_organ": False}, "no_organ"),
        ({"halted": True}, "halted"),
        ({"busy": True}, "busy"),
        ({"effective_tier": EnergyTier.SAVING}, "tier"),
        ({"night": True}, "night"),
        ({"state": ExplorationState(explorations_today=3)}, "daily_limit"),
        ({"state": ExplorationState(last_started_at=NOW)}, "interval"),
        ({"interests": []}, "no_interest"),
    ],
)
def test_each_exploration_gate(changes: dict, reason: str) -> None:
    result = allowed(**changes)
    assert not result.allowed and result.blocked_by == reason


def test_exploration_gate_order_and_cautious_daily_limit() -> None:
    assert (
        allowed(config=ExplorationConfig(), has_search=False, has_organ=False).blocked_by
        == "disabled"
    )
    assert allowed(has_search=False, halted=True).blocked_by == "no_search"
    assert daily_limit(ExplorationConfig(max_per_day=3), cautious=True) == 1
    assert daily_limit(ExplorationConfig(max_per_day=1), cautious=True) == 1
    assert daily_limit(ExplorationConfig(), cautious=False) == 3
    assert allowed(state=ExplorationState(last_started_at=NOW - timedelta(hours=4))).allowed


def test_select_never_explored_then_oldest_and_break_ties_by_id() -> None:
    entries = [InterestStatus(3, NOW), InterestStatus(2), InterestStatus(1)]
    assert allowed(interests=entries).interest_id == 1
    entries = [
        InterestStatus(3, NOW),
        InterestStatus(2, NOW - timedelta(days=1)),
        InterestStatus(1, NOW),
    ]
    assert allowed(interests=entries).interest_id == 2


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"interesting": True},
        {"interesting": "true", "finding": "x"},
        {"interesting": True, "finding": ""},
        {"interesting": True, "finding": "x" * 201},
        {"interesting": False, "finding": 4},
        {"interesting": True, "finding": "x", "action": "run"},
    ],
)
def test_summary_rejects_invalid_shape_without_repairs(data: Any) -> None:
    assert parse_summary(json.dumps(data)) is None


def test_summary_accepts_code_fence_and_trims() -> None:
    result = parse_summary('说明 {"interesting":true,"finding":" 收获 "}')
    assert result is not None and result.finding == "收获"
    boring = parse_summary('{"interesting":false,"finding":""}')
    assert boring is not None and boring.interesting is False
    assert parse_summary(" " * 4001 + '{"interesting":false,"finding":""}') == boring
