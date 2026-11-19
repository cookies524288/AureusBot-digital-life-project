"""激素对行为的影响：回击、冷处理、低落缩短、惊讶、事实摘要（D69 第 3 条）。"""

from datetime import UTC, datetime, timedelta

import pytest

from aureus.common import HormonesConfig
from aureus.hormones import (
    Conflict,
    HormoneKind,
    Hormones,
    HormoneTrigger,
    RetaliationMode,
)
from aureus.hormones.effects import compute_effects
from aureus.timeline import VisibilityScope

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
CONFIG = HormonesConfig(enabled=True)
PERSON = 7


def _trigger(
    kind: HormoneKind,
    rise: float,
    *,
    person_id: int | None = PERSON,
    reason: str = "被骂了",
    seconds_ago: float = 0.0,
) -> HormoneTrigger:
    return HormoneTrigger(
        kind=kind,
        rise=rise,
        triggered_at=NOW - timedelta(seconds=seconds_ago),
        person_id=person_id,
        reason=reason,
        source_record_id=1,
        source_visibility=VisibilityScope.USER_ONLY,
    )


def _effects(
    triggers: list[HormoneTrigger],
    conflict: Conflict = Conflict(),  # noqa: B008 冻结的数据类，可以当默认值
    *,
    person_id: int | None = PERSON,
    viewer_is_user: bool = True,
):
    return compute_effects(
        triggers,
        conflict,
        now=NOW,
        config=CONFIG,
        person_id=person_id,
        viewer_is_user=viewer_is_user,
    )


def test_calm_means_nothing_to_say() -> None:
    effects = _effects([])
    assert effects.summary == ""
    assert effects.retaliation is RetaliationMode.OFF
    assert not effects.may_cold_shoulder
    assert not effects.shorten_reply
    assert not effects.surprised
    assert effects.context_text(plan_first=True) == ""


def test_anger_below_the_retaliation_line_only_shows_in_the_summary() -> None:
    effects = _effects([_trigger(HormoneKind.ANGER, 0.3)])
    assert "怒气 0.30" in effects.summary
    assert effects.retaliation is RetaliationMode.OFF


def test_anger_at_the_line_allows_retaliation_with_guard_rails() -> None:
    effects = _effects([_trigger(HormoneKind.ANGER, 0.6)])
    assert effects.retaliation is RetaliationMode.ALLOWED
    text = effects.context_text(plan_first=False)
    assert "不许辱骂" in text
    assert "不回复" not in text


def test_retaliation_is_capped_by_code_not_by_the_model() -> None:
    used_up = Conflict(retaliations=CONFIG.max_retaliations)
    effects = _effects([_trigger(HormoneKind.ANGER, 0.6)], used_up)
    assert effects.retaliation is RetaliationMode.EXHAUSTED
    assert "不再回击" in effects.context_text(plan_first=False)


def test_cold_shoulder_is_offered_once_then_a_reply_is_required() -> None:
    angry = [_trigger(HormoneKind.ANGER, 0.9)]
    first = _effects(angry)
    assert first.may_cold_shoulder
    assert not first.must_reply
    assert "可以选择这次不回复" in first.context_text(plan_first=True)
    # 不先规划的档位没有规划这一步，不提冷处理。
    assert "不回复" not in first.context_text(plan_first=False)

    after_one_decline = _effects(angry, Conflict(declines=1))
    assert not after_one_decline.may_cold_shoulder
    assert after_one_decline.must_reply


def test_a_cooled_down_conflict_forgets_its_counts() -> None:
    effects = _effects([_trigger(HormoneKind.ANGER, 0.1)], Conflict(retaliations=5, declines=1))
    assert effects.retaliation is RetaliationMode.OFF
    assert not effects.must_reply
    assert effects.retaliations_used == 0


def test_anger_at_someone_else_does_not_turn_on_retaliation_here() -> None:
    effects = _effects([_trigger(HormoneKind.ANGER, 0.9, person_id=99)])
    assert effects.retaliation is RetaliationMode.OFF
    assert not effects.may_cold_shoulder


def test_unknown_person_gets_no_person_directed_behaviour() -> None:
    effects = _effects([_trigger(HormoneKind.ANGER, 0.9)], person_id=None)
    assert effects.retaliation is RetaliationMode.OFF
    assert not effects.must_reply


def test_hurt_shortens_and_surprise_is_flagged() -> None:
    effects = _effects(
        [
            _trigger(HormoneKind.HURT, 0.5, person_id=None),
            _trigger(HormoneKind.SURPRISE, 0.6, person_id=None),
        ]
    )
    assert effects.shorten_reply
    assert effects.surprised


def test_summary_hides_details_of_another_conversation_from_other_people() -> None:
    trigger = _trigger(HormoneKind.ANGER, 0.5, person_id=99, reason="说了一件私密的事")
    to_user = _effects([trigger], viewer_is_user=True)
    to_other = _effects([trigger], viewer_is_user=False)
    assert "说了一件私密的事" in to_user.summary
    assert "说了一件私密的事" not in to_other.summary
    assert "另一段对话里的事" in to_other.summary


def test_summary_shows_the_detail_in_the_same_conversation() -> None:
    trigger = _trigger(HormoneKind.ANGER, 0.5, reason="说了一件私密的事")
    other = _effects([trigger], viewer_is_user=False)
    assert "说了一件私密的事" in other.summary


def test_faded_hormones_are_left_out_of_the_summary() -> None:
    old = _trigger(HormoneKind.ANGER, 0.4, seconds_ago=60 * 60 * 24)
    assert _effects([old]).summary == ""


def test_disabled_kind_is_left_out_of_the_summary() -> None:
    config = HormonesConfig(enabled=True, anger_enabled=False)
    effects = compute_effects(
        [_trigger(HormoneKind.ANGER, 0.9)],
        Conflict(),
        now=NOW,
        config=config,
        person_id=PERSON,
        viewer_is_user=True,
    )
    assert effects.summary == ""
    assert effects.retaliation is RetaliationMode.OFF


# ---- Hormones 里的冲突计数 ----


def _hormones_with_anger(rise: float) -> Hormones:
    hormones = Hormones(CONFIG)
    hormones.add([_trigger(HormoneKind.ANGER, rise)], NOW)
    return hormones


def test_hormones_counts_retaliations_and_declines() -> None:
    hormones = _hormones_with_anger(0.6)
    for _ in range(CONFIG.max_retaliations):
        hormones.note_reply(PERSON, NOW, retaliated=True)
    effects = hormones.effects(NOW, person_id=PERSON, viewer_is_user=True)
    assert effects.retaliation is RetaliationMode.EXHAUSTED

    hormones.note_decline(PERSON, NOW)
    assert hormones.effects(NOW, person_id=PERSON, viewer_is_user=True).must_reply
    # 回复一次后，连续不回的计数清零。
    hormones.note_reply(PERSON, NOW, retaliated=False)
    assert not hormones.effects(NOW, person_id=PERSON, viewer_is_user=True).must_reply


def test_counts_reset_when_the_conflict_ends() -> None:
    hormones = _hormones_with_anger(0.6)
    hormones.note_reply(PERSON, NOW, retaliated=True)
    later = NOW + timedelta(days=1)
    hormones.note_reply(PERSON, later, retaliated=False)
    hormones.add([_trigger(HormoneKind.ANGER, 0.6, seconds_ago=-86400)], later)
    assert hormones.effects(later, person_id=PERSON, viewer_is_user=True).retaliations_used == 0


def test_extra_triggers_count_without_changing_memory() -> None:
    hormones = Hormones(CONFIG)
    extra = [_trigger(HormoneKind.ANGER, 0.6)]
    effects = hormones.effects(NOW, person_id=PERSON, viewer_is_user=True, extra=extra)
    assert effects.retaliation is RetaliationMode.ALLOWED
    assert hormones.level(HormoneKind.ANGER, NOW, PERSON) == pytest.approx(0.0)


def test_notes_for_an_unknown_person_do_nothing() -> None:
    hormones = _hormones_with_anger(0.6)
    hormones.note_reply(None, NOW, retaliated=True)
    hormones.note_decline(None, NOW)
    assert hormones.effects(NOW, person_id=PERSON, viewer_is_user=True).retaliations_used == 0
