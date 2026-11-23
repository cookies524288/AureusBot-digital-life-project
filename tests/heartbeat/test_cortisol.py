"""3.8 的时相、反馈驱动和重启重建，全部使用临时数据库。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aureus.common import ManualClock
from aureus.common.config import CortisolConfig
from aureus.feedback import COMMITMENT_KEPT, PROACTIVE_CONTACT, FeedbackStore, Outcome
from aureus.heartbeat.cortisol import CortisolTiming


def test_local_phase_repeats_across_midnight_and_offset_change() -> None:
    config = CortisolConfig(enabled=True)
    zone = ZoneInfo("Europe/Paris")
    timing = CortisolTiming(config, zone, None)
    trough = datetime(2026, 3, 28, 0, 18, tzinfo=zone)
    peak = trough.replace(hour=8, minute=32)
    assert timing.level(trough.astimezone(UTC)) == 0.0
    assert timing.level(peak.astimezone(UTC)) == pytest.approx(config.baseline_span)
    assert timing.level((peak + timedelta(days=1)).astimezone(UTC)) == timing.level(peak)
    midnight = trough.replace(hour=0, minute=0) + timedelta(days=1)
    assert timing.level(midnight - timedelta(seconds=1)) == pytest.approx(
        timing.level(midnight + timedelta(seconds=1)), abs=1e-5
    )


def test_only_first_sourced_settled_commitment_failure_drives_each_pulse(tmp_path: Path) -> None:
    start = datetime(2026, 11, 22, 8, 22, tzinfo=UTC)
    clock = ManualClock(start)
    path = tmp_path / "feedback.db"
    feedback = FeedbackStore(path, clock=clock)
    config = CortisolConfig(enabled=True, baseline_span=0.0)
    timing = CortisolTiming(config, UTC, feedback)
    feedback.open()

    def record(category: str, source_id: int | None, outcome: Outcome) -> None:
        row = feedback.record_expectation(category, 0.8, source_id)
        feedback.record_outcome(row, outcome)

    try:
        feedback.record_expectation(COMMITMENT_KEPT, 0.8, 1)
        record(COMMITMENT_KEPT, 2, Outcome.SUCCESS)
        record(COMMITMENT_KEPT, None, Outcome.FAILURE)
        record(PROACTIVE_CONTACT, 3, Outcome.FAILURE)
        rise = timedelta(seconds=config.rise_seconds)
        half_life = timedelta(seconds=config.half_life_seconds)
        assert timing.level(start + rise) == 0.0
        record(COMMITMENT_KEPT, 4, Outcome.FAILURE)
        record(COMMITMENT_KEPT, 4, Outcome.FAILURE)
        assert timing.level(start - timedelta(seconds=1)) == 0.0
        assert timing.level(start) == 0.0
        assert timing.level(start + rise / 2) == pytest.approx(config.failure_peak / 2)
        assert timing.level(start + rise) == pytest.approx(config.failure_peak)
        assert timing.level(start + rise + half_life) == pytest.approx(config.failure_peak / 2)
        failures = feedback.commitment_failures(start, start)
        assert [(row.source_id, row.outcome_at) for row in failures] == [(4, start)]

        clock.advance(rise + half_life * 13)
        record(COMMITMENT_KEPT, 4, Outcome.FAILURE)
        assert timing.level(clock.now() + rise) == 0.0
        record(COMMITMENT_KEPT, 5, Outcome.FAILURE)
        record(COMMITMENT_KEPT, 6, Outcome.FAILURE)
        sample = clock.now() + rise
        assert timing.level(sample) == 1.0
        assert timing.level(sample + half_life) == pytest.approx(config.failure_peak)
    finally:
        feedback.close()

    reopened = FeedbackStore(path, clock=clock)
    reopened.open()
    try:
        rebuilt = CortisolTiming(config, UTC, reopened)
        assert rebuilt.level(sample) == 1.0
        assert rebuilt.level(sample + half_life) == pytest.approx(config.failure_peak)
    finally:
        reopened.close()
    # 关闭时连反馈存储也无需打开。
    assert CortisolTiming(CortisolConfig(), UTC, reopened).level(sample) == 0.0
