"""按精力档位选回复做法的测试（路线图 1.6，D59）：不同档位的调用次数、模型、上下文长度。"""

import json

import pytest

from aureus.body import EnergyTier
from aureus.common import HeartbeatConfig
from aureus.heartbeat import ReplyLength, choose_strategy, judge_allowed
from aureus.organs import ModelPreference


@pytest.mark.parametrize("tier", [None, EnergyTier.NORMAL, EnergyTier.UNLIMITED])
def test_normal_tier_plans_first_with_the_preferred_model(tier: EnergyTier | None) -> None:
    config = HeartbeatConfig()
    strategy = choose_strategy(tier, config)
    assert strategy.plan_first
    assert strategy.model_preference is ModelPreference.PREFERRED
    assert strategy.message_limit == config.recent_message_limit
    assert strategy.length_limit is None


def test_legacy_planning_switch_keeps_planning() -> None:
    strategy = choose_strategy(EnergyTier.NORMAL, HeartbeatConfig(planning_enabled=False))
    assert strategy.plan_first
    assert strategy.model_preference is ModelPreference.PREFERRED


def test_saving_tier_plans_and_shortens_the_reply() -> None:
    config = HeartbeatConfig()
    strategy = choose_strategy(EnergyTier.SAVING, config)
    assert strategy.plan_first
    assert strategy.model_preference is ModelPreference.CHEAPER
    assert strategy.message_limit == config.saving_message_limit
    assert strategy.length_limit is ReplyLength.MEDIUM


@pytest.mark.parametrize("tier", [EnergyTier.LOW, EnergyTier.OVERDRAFT])
def test_low_tiers_use_the_cheapest_model_and_the_shortest_reply(tier: EnergyTier) -> None:
    config = HeartbeatConfig()
    strategy = choose_strategy(tier, config)
    assert strategy.plan_first
    assert strategy.model_preference is ModelPreference.CHEAPEST
    assert strategy.message_limit == config.low_message_limit
    assert strategy.length_limit is ReplyLength.SHORT


def test_lower_tiers_never_cost_more_than_higher_ones() -> None:
    config = HeartbeatConfig()
    tiers = [EnergyTier.NORMAL, EnergyTier.SAVING, EnergyTier.LOW]
    limits = [choose_strategy(tier, config).message_limit for tier in tiers]
    assert limits == sorted(limits, reverse=True)


@pytest.mark.parametrize("tier", [EnergyTier.SAVING, EnergyTier.LOW, EnergyTier.OVERDRAFT])
def test_tier_limits_never_exceed_the_configured_recent_limit(tier: EnergyTier) -> None:
    strategy = choose_strategy(tier, HeartbeatConfig(recent_message_limit=3))
    assert strategy.message_limit == 3


def test_description_states_what_is_actually_done() -> None:
    normal = choose_strategy(EnergyTier.NORMAL, HeartbeatConfig()).describe()
    assert "先想好要不要回" in normal
    assert "首选" in normal
    assert "最近 20 条" in normal
    assert "回复控制" not in normal
    assert "更便宜的才会换" not in normal

    saving = choose_strategy(EnergyTier.SAVING, HeartbeatConfig()).describe()
    assert "先想好要不要回" in saving
    assert "便宜一档" in saving
    assert "最近 10 条" in saving
    assert "三五句话" in saving
    assert "更便宜的才会换" in saving

    low = choose_strategy(EnergyTier.LOW, HeartbeatConfig()).describe()
    assert "最便宜" in low
    assert "一两句话" in low


def test_metadata_is_plain_json() -> None:
    metadata = choose_strategy(EnergyTier.SAVING, HeartbeatConfig()).to_metadata()
    assert metadata == {
        "energy_tier": "节省",
        "plan_first": True,
        "model_preference": "便宜一档",
        "message_limit": 10,
        "length_limit": "中",
        "stress_lowered": False,
    }
    json.dumps(metadata, ensure_ascii=False)
    assert choose_strategy(None, HeartbeatConfig()).to_metadata()["energy_tier"] is None


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (None, True),  # 没有身体读数按正常档处理
        (EnergyTier.UNLIMITED, True),
        (EnergyTier.NORMAL, True),
        (EnergyTier.SAVING, True),
        (EnergyTier.LOW, False),
        (EnergyTier.OVERDRAFT, False),
    ],
)
def test_judge_is_only_called_on_tiers_with_enough_energy(
    tier: EnergyTier | None, expected: bool
) -> None:
    assert judge_allowed(tier) is expected


def test_every_tier_has_an_explicit_judge_decision() -> None:
    # 新增档位时这里会提醒：要想清楚判断器官在新档位上调不调用。
    assert {tier for tier in EnergyTier if judge_allowed(tier)} == {
        EnergyTier.UNLIMITED,
        EnergyTier.NORMAL,
        EnergyTier.SAVING,
    }


@pytest.mark.parametrize(
    ("tier", "effective"),
    [
        (None, EnergyTier.SAVING),
        (EnergyTier.UNLIMITED, EnergyTier.SAVING),
        (EnergyTier.NORMAL, EnergyTier.SAVING),
        (EnergyTier.SAVING, EnergyTier.LOW),
        (EnergyTier.LOW, EnergyTier.LOW),
        (EnergyTier.OVERDRAFT, EnergyTier.OVERDRAFT),
    ],
)
def test_stress_lowers_by_one_tier_but_never_into_overdraft(
    tier: EnergyTier | None, effective: EnergyTier
) -> None:
    config = HeartbeatConfig()
    stressed = choose_strategy(tier, config, stress_high=True)
    expected = choose_strategy(effective, config)
    assert stressed.tier is tier  # 记的仍是精力档位本身
    assert stressed.effective_tier is effective
    assert stressed.plan_first == expected.plan_first
    assert stressed.model_preference == expected.model_preference
    assert stressed.length_limit == expected.length_limit
    assert stressed.stress_lowered == (effective is not tier)


def test_stress_lowering_is_explained_and_recorded() -> None:
    stressed = choose_strategy(EnergyTier.NORMAL, HeartbeatConfig(), stress_high=True)
    assert "压力偏高" in stressed.describe()
    assert stressed.to_metadata()["stress_lowered"] is True
    assert (
        choose_strategy(EnergyTier.NORMAL, HeartbeatConfig()).to_metadata()["stress_lowered"]
        is False
    )


def test_judge_follows_the_effective_tier() -> None:
    stressed = choose_strategy(EnergyTier.SAVING, HeartbeatConfig(), stress_high=True)
    assert not judge_allowed(stressed.effective_tier)  # 降到低电量就不判断
