"""激素计算的测试：半衰期、封顶、按人过滤（路线图 2.3 第 1 步，D69）。"""

from datetime import UTC, datetime, timedelta

import pytest

from aureus.common import HormonesConfig
from aureus.hormones import HormoneKind, HormoneTrigger, level_at
from aureus.timeline import VisibilityScope

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _trigger(
    kind: HormoneKind,
    rise: float,
    minutes_ago: float,
    person_id: int | None = None,
) -> HormoneTrigger:
    """造测试数据：只填计算用得到的字段。"""
    return HormoneTrigger(
        kind=kind,
        rise=rise,
        triggered_at=NOW - timedelta(minutes=minutes_ago),
        person_id=person_id,
        reason="test",
        source_record_id=1,
        source_visibility=VisibilityScope.PRIVATE,
    )


def test_level_at_sums_decayed_rises() -> None:
    """浓度 = 触发按半衰期衰减的和。"""
    config = HormonesConfig(anger_half_life_seconds=600.0)
    # 半衰期 10 分钟，10 分钟前涨 0.5，现在剩 0.25。
    triggers = [_trigger(HormoneKind.ANGER, 0.5, minutes_ago=10)]
    assert level_at(triggers, HormoneKind.ANGER, NOW, config) == pytest.approx(0.25, abs=1e-6)


def test_level_at_caps_at_one() -> None:
    """浓度封顶 1。"""
    config = HormonesConfig()
    # 很多次触发加起来超过 1，封顶。
    triggers = [_trigger(HormoneKind.JOY, 0.4, 0) for _ in range(10)]
    assert level_at(triggers, HormoneKind.JOY, NOW, config) == 1.0


def test_level_at_filters_by_kind() -> None:
    """只算指定种类。"""
    config = HormonesConfig()
    triggers = [
        _trigger(HormoneKind.ANGER, 0.3, 0),
        _trigger(HormoneKind.JOY, 0.5, 0),
    ]
    assert level_at(triggers, HormoneKind.ANGER, NOW, config) == pytest.approx(0.3)
    assert level_at(triggers, HormoneKind.JOY, NOW, config) == pytest.approx(0.5)


def test_level_at_filters_by_person_id() -> None:
    """给了 person_id 就只算这个人的触发。"""
    config = HormonesConfig()
    triggers = [
        _trigger(HormoneKind.ANGER, 0.2, 0, person_id=10),
        _trigger(HormoneKind.ANGER, 0.3, 0, person_id=20),
        _trigger(HormoneKind.ANGER, 0.4, 0, person_id=None),  # 不针对某个人
    ]
    assert level_at(triggers, HormoneKind.ANGER, NOW, config, person_id=10) == pytest.approx(0.2)
    assert level_at(triggers, HormoneKind.ANGER, NOW, config, person_id=20) == pytest.approx(0.3)
    # 没给 person_id，所有触发都算。
    assert level_at(triggers, HormoneKind.ANGER, NOW, config, person_id=None) == pytest.approx(0.9)


def test_level_at_ignores_future_triggers() -> None:
    """触发时刻在 now 之后的不算（不依赖顺序）。"""
    config = HormonesConfig()
    triggers = [
        _trigger(HormoneKind.HURT, 0.3, minutes_ago=-5),  # 未来
        _trigger(HormoneKind.HURT, 0.2, 0),
    ]
    assert level_at(triggers, HormoneKind.HURT, NOW, config) == pytest.approx(0.2)


def test_level_at_when_kind_disabled() -> None:
    """这种激素被关掉时浓度是 0。"""
    config = HormonesConfig(anger_enabled=False)
    triggers = [_trigger(HormoneKind.ANGER, 0.5, 0)]
    assert level_at(triggers, HormoneKind.ANGER, NOW, config) == 0.0


def test_level_at_when_hormones_disabled() -> None:
    """激素总开关关着时浓度是 0。"""
    config = HormonesConfig(enabled=False)
    triggers = [_trigger(HormoneKind.SURPRISE, 0.5, 0)]
    assert level_at(triggers, HormoneKind.SURPRISE, NOW, config) == 0.0


def test_level_at_independent_of_call_frequency() -> None:
    """结果和调用频率无关：同一批触发，隔 1 分钟算一次、隔 1 小时算一次，结果一样。"""
    config = HormonesConfig(joy_half_life_seconds=900.0)
    triggers = [_trigger(HormoneKind.JOY, 0.6, minutes_ago=30)]
    # 30 分钟后算一次。
    level_30 = level_at(triggers, HormoneKind.JOY, NOW, config)
    # 每隔 1 分钟算一次，最后一次也是 30 分钟后。
    current = NOW - timedelta(minutes=30)
    for _ in range(30):
        current += timedelta(minutes=1)
        _ = level_at(triggers, HormoneKind.JOY, current, config)
    level_after_many_calls = level_at(triggers, HormoneKind.JOY, NOW, config)
    assert level_after_many_calls == pytest.approx(level_30, abs=1e-9)


def test_level_at_with_zero_triggers() -> None:
    """没有触发时浓度是 0。"""
    config = HormonesConfig()
    assert level_at([], HormoneKind.ANGER, NOW, config) == 0.0


def test_trigger_to_metadata_can_be_serialized() -> None:
    """to_metadata() 的结果可以序列化成 JSON，字段齐全。"""
    trigger = HormoneTrigger(
        kind=HormoneKind.ANGER,
        rise=0.3,
        triggered_at=NOW,
        person_id=42,
        reason="被嘲笑是 AI",
        source_record_id=123,
        source_visibility=VisibilityScope.PRIVATE,
    )
    metadata = trigger.to_metadata()
    assert metadata == {
        "kind": "anger",
        "rise": 0.3,
        "triggered_at": NOW.isoformat(),
        "person_id": 42,
        "reason": "被嘲笑是 AI",
        "source_record_id": 123,
        "source_visibility": "private",
    }
