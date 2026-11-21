"""E2 精力开关对照和逐日账本用量的测试（D82）。"""

import logging
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from aureus.common import BudgetCalendar, ManualClock
from aureus.lab.energy import ENERGY_EXPERIMENT, EventKind, build_schedule, run_energy
from aureus.lab.experiment import LAB_START
from aureus.safety import Ledger

PARAMETERS = ENERGY_EXPERIMENT.parameters


def test_schedule_is_seeded_and_has_the_expected_daily_load() -> None:
    schedule = build_schedule(PARAMETERS, 0)

    assert schedule == build_schedule(PARAMETERS, 0)
    assert schedule != build_schedule(PARAMETERS, 1)
    assert len(schedule) == PARAMETERS["days"]
    for events in schedule:
        user_messages = events.count(EventKind.USER_MESSAGE)
        assert (
            int(PARAMETERS["user_messages_min"])
            <= user_messages
            <= int(PARAMETERS["user_messages_max"])
        )
        assert events.count(EventKind.OPTIONAL_WISH) == PARAMETERS["optional_wishes_per_day"]


@pytest.mark.asyncio
async def test_energy_on_avoids_starvation_and_daily_spend_matches_ledger(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="aureus")
    result = await run_energy(PARAMETERS, 0, tmp_path)

    assert result.metrics["on.starved_days"] == 0
    assert result.metrics["on.user_response_rate"] == 1.0
    assert int(result.metrics["on.month_spend"]) <= int(PARAMETERS["monthly_token_limit"])
    assert int(result.metrics["off.starved_days"]) > 0
    assert float(result.metrics["off.user_response_rate"]) < 1.0
    assert int(result.metrics["on.overspent_days"]) < int(result.metrics["off.overspent_days"])
    assert result.metrics["on.user_messages"] == result.metrics["off.user_messages"]

    (daily,) = result.tables
    assert daily.name == "daily"
    assert len(daily.rows) == 30
    assert daily.rows[-1][daily.columns.index("on_cumulative")] == result.metrics["on.month_spend"]
    assert (tmp_path / "energy_on.db").is_file()
    assert (tmp_path / "energy_off.db").is_file()

    ledger = Ledger(tmp_path / "energy_on.db", ManualClock(LAB_START), calendar=BudgetCalendar(UTC))
    ledger.open()
    try:
        recorded_spend = [
            ledger.tokens_used_in_day(LAB_START + timedelta(days=day, hours=12))
            for day in range(30)
        ]
    finally:
        ledger.close()
    on_spend = daily.columns.index("on_spend")
    assert [row[on_spend] for row in daily.rows] == recorded_spend
