"""身体状态摘要的测试：写出来的数字和原因必须和读数一致，关掉的信号不能说成正常。"""

from aureus.body import BodyReading, EnergyReading, EnergyTier
from aureus.body.signals import FatigueReading, SleepReading, StressReading
from aureus.common import BodyConfig
from aureus.heartbeat.body_summary import describe_body_state

CONFIG = BodyConfig()

LIMITED = EnergyReading(
    tier=EnergyTier.SAVING,
    fraction=0.35,
    daily_quota=1_000_000,
    used_today=650_000,
    remaining_today=350_000,
    month_remaining=12_000_000,
    days_left_in_month=12,
    optional_ceiling=850_000,
)
CALM = BodyReading(
    energy=LIMITED,
    fatigue=FatigueReading(enabled=True, level=0.8, calls_in_window=48, high=True),
    stress=StressReading(
        enabled=True,
        level=0.05,
        failure_rate=0.05,
        sampled_calls=20,
        failed_calls=1,
        backlog=0,
        recent_failure_reasons=("模型服务 503",),
    ),
    sleep=SleepReading(enabled=True, night=False),
)


def test_summary_states_the_real_numbers_and_reasons() -> None:
    text = describe_body_state(CALM, CONFIG)
    assert text.startswith("【身体状态】")
    assert "精力 35%（节省档）" in text
    assert "今日额度 1,000,000 token" in text
    assert "已用 650,000" in text
    assert "本月还剩 12,000,000 token、12 天" in text
    assert "疲劳 80%（偏高）" in text
    assert "最近 30 分钟调用了 48 次" in text
    assert "压力 5%（不高）" in text
    assert "最近 20 次调用失败了 1 次，排队的消息 0 条" in text
    assert "最近的失败原因：模型服务 503" in text
    assert "现在不是夜间时段" in text


def test_summary_tells_the_model_to_be_truthful_and_not_to_act() -> None:
    text = describe_body_state(CALM, CONFIG)
    assert "不要夸大" in text
    assert "不要编造" in text
    assert "不需要你再演出来" in text


def test_unlimited_energy_is_not_presented_as_having_energy() -> None:
    reading = BodyReading(
        energy=EnergyReading(tier=EnergyTier.UNLIMITED),
        fatigue=FatigueReading(enabled=False),
        stress=StressReading(enabled=False),
        sleep=SleepReading(enabled=False),
    )
    text = describe_body_state(reading, CONFIG)
    assert "精力 不限：没有按每日额度计算" in text
    assert "%" not in text.split("以上是你真实")[0]


def test_disabled_signals_are_left_out_not_reported_as_normal() -> None:
    reading = BodyReading(
        energy=LIMITED,
        fatigue=FatigueReading(enabled=False),
        stress=StressReading(enabled=False),
        sleep=SleepReading(enabled=False),
    )
    text = describe_body_state(reading, CONFIG)
    assert "疲劳" not in text.split("以上是你真实")[0]
    assert "压力" not in text.split("以上是你真实")[0]
    assert "夜间" not in text
    assert "精力 35%" in text


def test_night_is_reported_with_the_configured_hours() -> None:
    reading = BodyReading(
        energy=LIMITED,
        fatigue=FatigueReading(enabled=False),
        stress=StressReading(enabled=False),
        sleep=SleepReading(enabled=True, night=True),
    )
    text = describe_body_state(reading, BodyConfig(night_start_hour=23, night_end_hour=6))
    assert "现在是夜间时段（当地 23 点到 6 点）" in text
