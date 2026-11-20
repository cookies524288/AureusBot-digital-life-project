"""心情的测试（路线图 2.6，D73）：累积、回落、压力、谨慎滞后、原因、还原。"""

from datetime import UTC, datetime, timedelta

import pytest

from aureus.common import MoodConfig
from aureus.hormones import HormoneKind, HormoneTrigger
from aureus.mood import (
    FeedbackOutcome,
    Mood,
    MoodContribution,
    from_outcomes,
    from_triggers,
    valence_at,
)
from aureus.timeline import VisibilityScope

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CONFIG = MoodConfig()
HALF_LIFE = timedelta(seconds=CONFIG.half_life_seconds)


def _trigger(
    kind: HormoneKind,
    rise: float = 0.4,
    *,
    ago: timedelta = timedelta(0),
    person_id: int | None = 7,
    reason: str = "被冒犯了",
) -> HormoneTrigger:
    return HormoneTrigger(kind, rise, NOW - ago, person_id, reason, 1, VisibilityScope.USER_ONLY)


def _failures(count: int, *, ago: timedelta = timedelta(0)) -> list[FeedbackOutcome]:
    return [FeedbackOutcome(NOW - ago, False, "sim_task:hard") for _ in range(count)]


def _successes(count: int, *, ago: timedelta = timedelta(0)) -> list[FeedbackOutcome]:
    return [FeedbackOutcome(NOW - ago, True, "sim_task:hard") for _ in range(count)]


def test_hormone_directions() -> None:
    [joy] = from_triggers([_trigger(HormoneKind.JOY, reason="被夸了")], CONFIG)
    [anger] = from_triggers([_trigger(HormoneKind.ANGER)], CONFIG)
    [hurt] = from_triggers([_trigger(HormoneKind.HURT)], CONFIG)
    assert joy.delta == pytest.approx(0.2)
    assert anger.delta == pytest.approx(-0.2)
    assert hurt.delta == pytest.approx(-0.2)
    assert from_triggers([_trigger(HormoneKind.SURPRISE)], CONFIG) == []


def test_single_change_is_capped() -> None:
    config = MoodConfig(hormone_weight=5.0, failure_amount=0.9)
    [big] = from_triggers([_trigger(HormoneKind.ANGER, 1.0)], config)
    assert big.delta == pytest.approx(-config.max_single_change)
    [fail] = from_outcomes(_failures(1), config)
    assert fail.delta == pytest.approx(-config.max_single_change)


def test_outcomes_failures_weigh_more_than_successes() -> None:
    [win] = from_outcomes(_successes(1), CONFIG)
    [loss] = from_outcomes(_failures(1), CONFIG)
    assert win.delta == pytest.approx(0.05)
    assert loss.delta == pytest.approx(-0.1)
    assert "失败了" in loss.reason


def test_valence_decays_with_half_life_and_ignores_future() -> None:
    item = MoodContribution(NOW, -0.4, "x")
    assert valence_at([item], NOW, CONFIG) == pytest.approx(-0.4)
    assert valence_at([item], NOW + HALF_LIFE, CONFIG) == pytest.approx(-0.2)
    assert valence_at([MoodContribution(NOW + HALF_LIFE, -0.4, "x")], NOW, CONFIG) == 0.0


def test_valence_is_clamped() -> None:
    many = [MoodContribution(NOW, -0.2, "x") for _ in range(20)]
    assert valence_at(many, NOW, CONFIG) == -1.0
    assert valence_at([MoodContribution(NOW, 0.2, "x")] * 20, NOW, CONFIG) == 1.0


def test_stress_pulls_down_now_but_does_not_accumulate() -> None:
    assert valence_at([], NOW, CONFIG, stress_level=1.0) == pytest.approx(-0.3)
    assert valence_at([], NOW, CONFIG, stress_level=0.0) == 0.0
    assert valence_at([], NOW, CONFIG, stress_level=5.0) == pytest.approx(-0.3)  # 截到 1


def test_four_failures_make_it_cautious_then_recovery_has_hysteresis() -> None:
    mood = Mood(CONFIG)
    assert not mood.read(NOW, _failures(2)).cautious  # −0.2 还没到谨慎线
    reading = mood.read(NOW, _failures(4))
    assert reading.cautious
    assert "谨慎" in reading.summary and "sim_task:hard 失败了" in reading.summary
    # 回到 −0.2：比谨慎线高，但还没过恢复线，仍谨慎
    assert mood.read(NOW, _failures(2)).cautious
    # 回到 −0.1：过了恢复线，解除
    assert not mood.read(NOW, _failures(1)).cautious
    assert mood.cautious is False


def test_successes_and_time_bring_it_back() -> None:
    mood = Mood(CONFIG)
    assert mood.read(NOW, _failures(5)).cautious
    later = NOW + HALF_LIFE
    outcomes = [*_failures(5), *_successes(3, ago=-HALF_LIFE)]
    assert not mood.read(later, outcomes).cautious


def test_summary_names_main_reasons_and_hides_other_conversations() -> None:
    mood = Mood(CONFIG)
    mood.add_triggers([_trigger(HormoneKind.ANGER, person_id=99, reason="被骂了")], NOW)
    to_user = mood.read(NOW, viewer_is_user=True).summary
    to_other = mood.read(NOW, person_id=7, viewer_is_user=False).summary
    to_same = mood.read(NOW, person_id=99, viewer_is_user=False).summary
    assert "被骂了" in to_user
    assert "被骂了" not in to_other and "另一段对话里的事" in to_other
    assert "被骂了" in to_same
    assert "【心情】" in to_user and "偏低" in to_user


def test_stress_shows_up_as_a_reason() -> None:
    summary = Mood(CONFIG).read(NOW, stress_level=0.8).summary
    assert "此刻压力 80%" in summary


def test_restore_equals_running_and_counts_downtime() -> None:
    triggers = [
        _trigger(HormoneKind.ANGER, ago=timedelta(hours=1)),
        _trigger(HormoneKind.JOY, 0.2, ago=timedelta(minutes=5), reason="被夸了"),
    ]
    running = Mood(CONFIG)
    running.add_triggers(triggers, NOW)
    later = NOW + timedelta(hours=3)
    restarted = Mood(CONFIG)
    count = restarted.restore([t.to_metadata() for t in triggers] + [{"kind": "??"}], later)
    assert count == 2
    assert restarted.read(later).valence == pytest.approx(running.read(later).valence)


def test_old_contributions_are_forgotten() -> None:
    mood = Mood(CONFIG)
    old = _trigger(HormoneKind.ANGER, ago=mood.lookback() + timedelta(seconds=1))
    mood.add_triggers([old], NOW)
    assert mood.read(NOW).valence == 0.0
