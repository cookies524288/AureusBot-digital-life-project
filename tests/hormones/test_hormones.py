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


# ---------- 触发规则（路线图 2.3 第 3 步，D69 第 2 条） ----------

from aureus.heartbeat.judging import TowardMe, TowardMeKind  # noqa: E402
from aureus.hormones import (  # noqa: E402
    make_triggers_from_feedback,
    make_triggers_from_judgment,
)


def _judged(
    toward_me: list[TowardMe],
    config: HormonesConfig,
    recent: list[HormoneTrigger] | None = None,
    person_id: int = 7,
) -> list[HormoneTrigger]:
    return make_triggers_from_judgment(
        toward_me,
        person_id=person_id,
        source_record_id=5,
        source_visibility=VisibilityScope.USER_ONLY,
        now=NOW,
        config=config,
        recent_triggers=recent or [],
    )


@pytest.mark.parametrize(
    ("matter", "hormone"),
    [
        (TowardMeKind.OFFENSE, HormoneKind.ANGER),
        (TowardMeKind.PRAISE, HormoneKind.JOY),
        (TowardMeKind.REJECTION, HormoneKind.HURT),
    ],
)
def test_judgment_matters_map_to_hormones(matter: TowardMeKind, hormone: HormoneKind) -> None:
    config = HormonesConfig()
    [trigger] = _judged([TowardMe(matter, intensity=0.8, confidence=0.9)], config)
    assert trigger.kind == hormone
    assert trigger.person_id == 7
    assert trigger.source_record_id == 5
    assert trigger.source_visibility == VisibilityScope.USER_ONLY
    # 基础量 0.5 × 强度 0.8 × 把握 0.9
    assert trigger.rise == pytest.approx(0.36)


def test_judgment_rise_is_capped_by_max_single_rise() -> None:
    config = HormonesConfig(base_amount=1.0, max_single_rise=0.4)
    [trigger] = _judged([TowardMe(TowardMeKind.OFFENSE, 1.0, 1.0)], config)
    assert trigger.rise == pytest.approx(0.4)


def test_judgment_low_confidence_is_ignored() -> None:
    config = HormonesConfig(min_confidence=0.3)
    assert _judged([TowardMe(TowardMeKind.OFFENSE, 1.0, 0.29)], config) == []
    assert len(_judged([TowardMe(TowardMeKind.OFFENSE, 1.0, 0.3)], config)) == 1


def test_judgment_zero_intensity_makes_no_trigger() -> None:
    assert _judged([TowardMe(TowardMeKind.PRAISE, 0.0, 1.0)], HormonesConfig()) == []


def test_judgment_when_hormones_disabled() -> None:
    config = HormonesConfig(enabled=False)
    assert _judged([TowardMe(TowardMeKind.OFFENSE, 1.0, 1.0)], config) == []


def test_judgment_repeats_by_same_person_decay() -> None:
    """同一人在窗口里反复触发：已有 k 次，这次乘 repeat_decay 的 k 次方。"""
    config = HormonesConfig(base_amount=0.5, repeat_decay=0.6, max_single_rise=1.0)
    earlier = [_trigger(HormoneKind.ANGER, 0.3, minutes_ago=1, person_id=7) for _ in range(2)]
    [trigger] = _judged([TowardMe(TowardMeKind.OFFENSE, 1.0, 1.0)], config, earlier)
    assert trigger.rise == pytest.approx(0.5 * 0.6**2)


def test_judgment_repeats_ignore_other_people_other_kinds_and_old_triggers() -> None:
    config = HormonesConfig(
        base_amount=0.5, repeat_decay=0.6, repeat_window_seconds=600.0, max_single_rise=1.0
    )
    earlier = [
        _trigger(HormoneKind.ANGER, 0.3, minutes_ago=1, person_id=8),  # 别人
        _trigger(HormoneKind.HURT, 0.3, minutes_ago=1, person_id=7),  # 别的激素
        _trigger(HormoneKind.ANGER, 0.3, minutes_ago=11, person_id=7),  # 窗口外
    ]
    [trigger] = _judged([TowardMe(TowardMeKind.OFFENSE, 1.0, 1.0)], config, earlier)
    assert trigger.rise == pytest.approx(0.5)


def test_judgment_same_batch_counts_as_repeat() -> None:
    config = HormonesConfig(base_amount=0.5, repeat_decay=0.6, max_single_rise=1.0)
    first, second = _judged(
        [
            TowardMe(TowardMeKind.OFFENSE, 1.0, 1.0),
            TowardMe(TowardMeKind.OFFENSE, 1.0, 1.0),
        ],
        config,
    )
    assert first.rise == pytest.approx(0.5)
    assert second.rise == pytest.approx(0.3)


def _feedback(
    expected: float | None, succeeded: bool, config: HormonesConfig
) -> list[HormoneTrigger]:
    return make_triggers_from_feedback(
        expected=expected,
        succeeded=succeeded,
        source_record_id=9,
        source_visibility=VisibilityScope.PRIVATE,
        now=NOW,
        config=config,
    )


def test_feedback_better_than_expected_gives_joy() -> None:
    config = HormonesConfig(surprise_gap=0.5)
    [trigger] = _feedback(0.7, True, config)  # 差距 0.3，不够惊讶
    assert trigger.kind == HormoneKind.JOY
    assert trigger.person_id is None
    assert trigger.rise == pytest.approx(0.5 * 0.3)


def test_feedback_worse_than_expected_gives_hurt() -> None:
    config = HormonesConfig(surprise_gap=0.5)
    [trigger] = _feedback(0.3, False, config)  # 差距 0.3
    assert trigger.kind == HormoneKind.HURT
    assert trigger.rise == pytest.approx(0.5 * 0.3)


def test_feedback_big_gap_adds_surprise() -> None:
    config = HormonesConfig(surprise_gap=0.5, base_amount=0.5)
    kinds = [trigger.kind for trigger in _feedback(0.9, False, config)]
    assert kinds == [HormoneKind.HURT, HormoneKind.SURPRISE]
    kinds = [trigger.kind for trigger in _feedback(0.1, True, config)]
    assert kinds == [HormoneKind.JOY, HormoneKind.SURPRISE]


def test_feedback_gap_exactly_at_threshold_is_surprising() -> None:
    config = HormonesConfig(surprise_gap=0.5)
    assert HormoneKind.SURPRISE in [t.kind for t in _feedback(0.5, True, config)]


def test_feedback_impulse_and_exact_match_give_nothing() -> None:
    config = HormonesConfig()
    assert _feedback(None, True, config) == []  # 冲动：没有预期就谈不上意外
    assert _feedback(1.0, True, config) == []  # 正好符合预期
    assert _feedback(0.0, False, config) == []


def test_feedback_when_hormones_disabled() -> None:
    assert _feedback(0.1, True, HormonesConfig(enabled=False)) == []
