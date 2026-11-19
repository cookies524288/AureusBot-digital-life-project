"""观察台激素曲线的测试：从触发记录算曲线，只读（D69 第 8 条）。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import HormonesConfig, ManualClock
from aureus.heartbeat.events import EVENT_HORMONE_TRIGGERED
from aureus.hormones import HormoneKind, HormoneTrigger
from aureus.monitor import MonitorReader
from aureus.monitor.hormone_curves import hormone_curves, lookback
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope

END = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
CONFIG = HormonesConfig()


def record(kind: HormoneKind, rise: float, at: datetime) -> dict[str, object]:
    return HormoneTrigger(
        kind=kind,
        rise=rise,
        triggered_at=at,
        person_id=None,
        reason="测试",
        source_record_id=1,
        source_visibility=VisibilityScope.PRIVATE,
    ).to_metadata()


def test_curve_rises_at_the_trigger_and_then_decays() -> None:
    trigger_at = END - timedelta(minutes=30)
    moments, curves = hormone_curves(
        [record(HormoneKind.ANGER, 0.3, trigger_at)], END - timedelta(hours=1), END, CONFIG, 7
    )
    anger = curves[HormoneKind.ANGER]
    assert len(moments) == len(anger) == 7
    assert anger[0] == 0.0
    assert anger[3] == pytest.approx(0.3)
    assert anger[4] < anger[3]
    assert curves[HormoneKind.JOY] == [0.0] * 7


def test_unreadable_records_are_skipped() -> None:
    _, curves = hormone_curves([{"kind": "nonsense"}], END - timedelta(hours=1), END, CONFIG, 3)
    assert all(level == 0.0 for levels in curves.values() for level in levels)


def test_bad_arguments_are_rejected() -> None:
    with pytest.raises(ValueError):
        hormone_curves([], END, END, CONFIG)
    with pytest.raises(ValueError):
        hormone_curves([], END - timedelta(hours=1), END, CONFIG, 1)


def test_lookback_covers_the_window_and_the_afterglow() -> None:
    assert lookback(24, CONFIG) > timedelta(hours=24)


def test_reader_returns_trigger_metadata_and_skips_other_events(tmp_path: Path) -> None:
    db_path = tmp_path / "aureus.db"
    db = TimelineDatabase(db_path, ManualClock(END))
    db.open()
    for event, metadata in (
        (EVENT_HORMONE_TRIGGERED, record(HormoneKind.JOY, 0.2, END)),
        ("judgment", {"x": 1}),
    ):
        db.append(event, "x", ActorType.SYSTEM, "test", VisibilityScope.PRIVATE, metadata)
    db.close()
    reader = MonitorReader(db_path)
    reader.open()
    try:
        records = reader.hormone_trigger_records(END - timedelta(hours=1))
        assert len(records) == 1
        assert records[0]["kind"] == "joy"
    finally:
        reader.close()
