"""同理心的测试（路线图 2.5，D72）：好感调节、限幅、递减、共情怒气不触发回击。"""

from datetime import UTC, datetime, timedelta

import pytest

from aureus.common import EmpathyConfig, HormonesConfig
from aureus.heartbeat.judging import OtherEmotion, OtherEmotionKind
from aureus.hormones import (
    HormoneKind,
    Hormones,
    HormoneTrigger,
    RetaliationMode,
    level_at,
    make_triggers_from_empathy,
)
from aureus.hormones.effects import Conflict, compute_effects
from aureus.timeline import VisibilityScope

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
HORMONES = HormonesConfig(enabled=True, max_single_rise=1.0)
EMPATHY = EmpathyConfig()
PERSON = 7


def _sad(subject: str = "sender", intensity: float = 1.0, confidence: float = 1.0) -> OtherEmotion:
    return OtherEmotion(subject, OtherEmotionKind.HURT, intensity, confidence)


def _empathize(
    others: list[OtherEmotion],
    affinity: float = 50.0,
    recent: list[HormoneTrigger] | None = None,
    hormones: HormonesConfig = HORMONES,
    empathy: EmpathyConfig = EMPATHY,
) -> list[HormoneTrigger]:
    return make_triggers_from_empathy(
        others,
        person_id=PERSON,
        affinity_of=lambda _subject: affinity,
        source_record_id=5,
        source_visibility=VisibilityScope.USER_ONLY,
        now=NOW,
        config=hormones,
        empathy=empathy,
        recent_triggers=recent or [],
    )


def test_same_emotion_rises_scaled_by_affinity() -> None:
    [close] = _empathize([_sad()], affinity=80.0)
    [stranger] = _empathize([_sad()], affinity=30.0)
    assert close.kind is HormoneKind.HURT
    assert close.rise == pytest.approx(0.3 * (0.2 + 0.8 * 0.8))  # 0.252
    assert stranger.rise == pytest.approx(0.3 * (0.2 + 0.8 * 0.3))  # 0.132
    assert close.rise > stranger.rise


def test_never_turns_into_schadenfreude_even_at_zero_affinity() -> None:
    [trigger] = _empathize([_sad()], affinity=0.0)
    assert trigger.rise == pytest.approx(0.3 * 0.2)
    assert trigger.rise > 0


def test_trigger_records_who_and_why() -> None:
    [trigger] = _empathize([_sad("我家猫", 0.5, 0.8)], affinity=30.0)
    assert trigger.empathy_for == "我家猫"
    assert trigger.empathy_affinity == 30.0
    assert trigger.reason == "替我家猫难过"
    assert trigger.person_id == PERSON  # 刷屏递减按发消息的人数
    assert _empathize([_sad()])[0].reason == "替对方难过"
    assert trigger.is_empathy


def test_each_emotion_maps_to_the_same_hormone() -> None:
    for emotion in OtherEmotionKind:
        [trigger] = _empathize([OtherEmotion("sender", emotion, 1.0, 1.0)])
        assert trigger.kind is HormoneKind(emotion.value)


def test_low_confidence_and_disabled_give_nothing() -> None:
    assert _empathize([_sad(confidence=HORMONES.min_confidence - 0.01)]) == []
    assert _empathize([_sad()], empathy=EmpathyConfig(enabled=False)) == []
    assert _empathize([_sad()], hormones=HormonesConfig(enabled=False)) == []
    assert _empathize([_sad(intensity=0.0)]) == []


def test_single_rise_is_capped() -> None:
    capped = HormonesConfig(enabled=True, max_single_rise=0.1)
    [trigger] = _empathize([_sad()], affinity=100.0, hormones=capped)
    assert trigger.rise == pytest.approx(0.1)


def test_spamming_decays_and_stays_bounded() -> None:
    recent: list[HormoneTrigger] = []
    rises = []
    for _ in range(10):
        [trigger] = _empathize([_sad()], affinity=100.0, recent=recent)
        recent.append(trigger)
        rises.append(trigger.rise)
    assert rises == sorted(rises, reverse=True)
    assert rises[-1] < rises[0] * 0.05
    assert level_at(recent, HormoneKind.HURT, NOW, HORMONES) <= 1.0
    assert sum(rises) < rises[0] / (1 - HORMONES.repeat_decay) + 1e-9  # 等比数列的上界


def test_same_batch_counts_as_repeat() -> None:
    first, second = _empathize([_sad(), _sad()], affinity=100.0)
    assert second.rise == pytest.approx(first.rise * HORMONES.repeat_decay)


def test_empathy_and_direct_triggers_are_counted_separately() -> None:
    direct = HormoneTrigger(
        HormoneKind.HURT, 0.3, NOW, PERSON, "被拒绝了", 1, VisibilityScope.USER_ONLY
    )
    [trigger] = _empathize([_sad()], affinity=100.0, recent=[direct])
    assert trigger.rise == pytest.approx(0.3 * 1.0)  # 没有被直接触发拖着递减


def test_empathy_anger_does_not_start_a_fight() -> None:
    angry = OtherEmotion("sender", OtherEmotionKind.ANGER, 1.0, 1.0)
    [trigger] = _empathize([angry], affinity=100.0, hormones=HormonesConfig(enabled=True))
    assert level_at([trigger], HormoneKind.ANGER, NOW, HORMONES) > 0.2  # 浓度和摘要里算
    assert level_at([trigger], HormoneKind.ANGER, NOW, HORMONES, exclude_empathy=True) == 0.0

    config = HormonesConfig(enabled=True, retaliate_at=0.25, cold_shoulder_at=0.3)
    effects = compute_effects(
        [trigger],
        Conflict(),
        now=NOW,
        config=config,
        person_id=PERSON,
        viewer_is_user=True,
    )
    assert effects.retaliation is RetaliationMode.OFF
    assert not effects.may_cold_shoulder
    assert "怒气" in effects.summary and "替对方生气" in effects.summary


def test_direct_anger_still_counts_when_mixed_with_empathy() -> None:
    config = HormonesConfig(enabled=True, retaliate_at=0.25, cold_shoulder_at=0.9)
    mixed = [
        HormoneTrigger(HormoneKind.ANGER, 0.3, NOW, PERSON, "被冒犯了", 1, VisibilityScope.PRIVATE),
        HormoneTrigger(
            HormoneKind.ANGER,
            0.5,
            NOW,
            PERSON,
            "替对方生气",
            1,
            VisibilityScope.PRIVATE,
            empathy_for="sender",
            empathy_affinity=50.0,
        ),
    ]
    effects = compute_effects(
        mixed, Conflict(), now=NOW, config=config, person_id=PERSON, viewer_is_user=True
    )
    assert effects.person_anger == pytest.approx(0.3)
    assert effects.retaliation is RetaliationMode.ALLOWED


def test_conflict_ends_when_only_empathy_anger_is_left() -> None:
    hormones = Hormones(HormonesConfig(enabled=True))
    hormones.add(
        [
            HormoneTrigger(
                HormoneKind.ANGER,
                0.5,
                NOW,
                PERSON,
                "替对方生气",
                1,
                VisibilityScope.PRIVATE,
                empathy_for="sender",
                empathy_affinity=50.0,
            )
        ],
        NOW,
    )
    hormones.note_reply(PERSON, NOW, retaliated=True)
    effects = hormones.effects(NOW, person_id=PERSON, viewer_is_user=True)
    assert effects.retaliations_used == 0


def test_metadata_round_trip_keeps_empathy_fields() -> None:
    [trigger] = _empathize([_sad("小橘", 0.6, 0.9)], affinity=42.0)
    assert HormoneTrigger.from_metadata(trigger.to_metadata()) == trigger
    plain = HormoneTrigger(HormoneKind.JOY, 0.2, NOW, None, "被夸了", 1, VisibilityScope.PRIVATE)
    assert "empathy_for" not in plain.to_metadata()  # 老记录的样子不变
    assert HormoneTrigger.from_metadata(plain.to_metadata()) == plain


@pytest.mark.parametrize(
    "change", [{"empathy_for": 5}, {"empathy_affinity": "高"}, {"empathy_affinity": True}]
)
def test_bad_empathy_metadata_is_unreadable(change: dict[str, object]) -> None:
    [trigger] = _empathize([_sad()])
    assert HormoneTrigger.from_metadata({**trigger.to_metadata(), **change}) is None


def test_old_records_without_empathy_fields_still_load() -> None:
    metadata = {
        "kind": "anger",
        "rise": 0.3,
        "triggered_at": (NOW - timedelta(minutes=1)).isoformat(),
        "person_id": 3,
        "reason": "被冒犯了",
        "source_record_id": 2,
        "source_visibility": "private",
    }
    trigger = HormoneTrigger.from_metadata(metadata)
    assert trigger is not None and not trigger.is_empathy
