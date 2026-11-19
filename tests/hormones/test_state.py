"""Hormones 内存状态和触发记录的还原（D69 第 1、5 条）。"""

from datetime import UTC, datetime, timedelta

import pytest

from aureus.common import HormonesConfig
from aureus.hormones import HormoneKind, Hormones, HormoneTrigger
from aureus.timeline import VisibilityScope

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _trigger(
    kind: HormoneKind = HormoneKind.ANGER,
    rise: float = 0.4,
    seconds_ago: float = 0.0,
    person_id: int | None = 7,
) -> HormoneTrigger:
    return HormoneTrigger(
        kind=kind,
        rise=rise,
        triggered_at=NOW - timedelta(seconds=seconds_ago),
        person_id=person_id,
        reason="被嘲笑是 AI",
        source_record_id=3,
        source_visibility=VisibilityScope.USER_ONLY,
    )


def test_metadata_round_trip() -> None:
    trigger = _trigger()
    assert HormoneTrigger.from_metadata(trigger.to_metadata()) == trigger
    unnamed = _trigger(person_id=None)
    assert HormoneTrigger.from_metadata(unnamed.to_metadata()) == unnamed


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "love"},
        {"rise": "0.4"},
        {"rise": True},
        {"rise": float("nan")},
        {"rise": -0.1},
        {"triggered_at": "昨天"},
        {"triggered_at": 5},
        {"person_id": "7"},
        {"person_id": True},
        {"reason": None},
        {"source_record_id": 1.5},
        {"source_visibility": "everyone"},
    ],
)
def test_metadata_that_cannot_be_read_gives_none(change: dict[str, object]) -> None:
    metadata = _trigger().to_metadata() | change
    assert HormoneTrigger.from_metadata(metadata) is None


def test_metadata_missing_field_gives_none() -> None:
    metadata = _trigger().to_metadata()
    del metadata["reason"]
    assert HormoneTrigger.from_metadata(metadata) is None


def test_level_decays_with_half_life() -> None:
    config = HormonesConfig()
    hormones = Hormones(config)
    hormones.add([_trigger(rise=0.4)], NOW)
    assert hormones.level(HormoneKind.ANGER, NOW) == pytest.approx(0.4)
    later = NOW + timedelta(seconds=config.anger_half_life_seconds)
    assert hormones.level(HormoneKind.ANGER, later) == pytest.approx(0.2)
    assert hormones.level(HormoneKind.JOY, NOW) == 0.0


def test_level_can_be_limited_to_a_person() -> None:
    hormones = Hormones(HormonesConfig())
    hormones.add([_trigger(person_id=7), _trigger(person_id=8, rise=0.3)], NOW)
    assert hormones.level(HormoneKind.ANGER, NOW, person_id=8) == pytest.approx(0.3)
    assert hormones.level(HormoneKind.ANGER, NOW) == pytest.approx(0.7)


def test_faded_triggers_are_forgotten() -> None:
    config = HormonesConfig()
    hormones = Hormones(config)
    old = _trigger(seconds_ago=hormones.lookback().total_seconds() + 1)
    hormones.add([old, _trigger(seconds_ago=1)], NOW)
    assert len(hormones.triggers) == 1


def test_restore_equals_continuous_run() -> None:
    """重启后浓度和没重启、一直按时间消退的结果一致（含停机的时间）。"""
    config = HormonesConfig()
    running = Hormones(config)
    triggers = [_trigger(seconds_ago=900), _trigger(HormoneKind.JOY, 0.3, seconds_ago=60)]
    running.add(triggers, NOW)
    later = NOW + timedelta(hours=1)  # 停机一小时
    restarted = Hormones(config)
    count = restarted.restore([t.to_metadata() for t in triggers], later)
    assert count == 2
    for kind in HormoneKind:
        assert restarted.level(kind, later) == pytest.approx(running.level(kind, later))


def test_restore_skips_unreadable_records_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    hormones = Hormones(HormonesConfig())
    good = _trigger().to_metadata()
    with caplog.at_level("WARNING"):
        count = hormones.restore([good, {"kind": "???"}, {}], NOW)
    assert count == 1
    assert caplog.text.count("看不懂") == 2


def test_restore_replaces_existing_state() -> None:
    hormones = Hormones(HormonesConfig())
    hormones.add([_trigger()], NOW)
    hormones.restore([], NOW)
    assert hormones.triggers == ()


def test_disabled_hormones_have_no_level() -> None:
    hormones = Hormones(HormonesConfig(enabled=False))
    hormones.add([_trigger()], NOW)
    assert hormones.level(HormoneKind.ANGER, NOW) == 0.0
