"""生物模型单元测试"""

from datetime import UTC, datetime

import pytest

from aureus.common.clock import ManualClock
from aureus.core.biomodel import (
    BiologicalState,
    CircadianModel,
    EmotionalState,
    HormoneState,
    UltradinRhythm,
)


class TestCircadianModel:
    """日周期模型测试"""

    def test_phase_at_time(self):
        """测试计算日周期相位"""
        circadian = CircadianModel()

        # 午夜（0:00） → phase = 0.0
        midnight = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert abs(circadian.phase_at_time(midnight) - 0.0) < 0.01

        # 中午（12:00） → phase = 0.5
        noon = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert abs(circadian.phase_at_time(noon) - 0.5) < 0.01

        # 下午 6 点（18:00） → phase = 0.75
        evening = datetime(2026, 1, 1, 18, 0, 0, tzinfo=UTC)
        assert abs(circadian.phase_at_time(evening) - 0.75) < 0.01

    def test_cortisol_factor_peaks_at_morning(self):
        """测试皮质醇在早晨达到高峰"""
        circadian = CircadianModel()

        # 凌晨 8 点应该最高
        morning = datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC)
        factor_8am = circadian.circadian_factor_for_cortisol(circadian.phase_at_time(morning))
        assert factor_8am > 1.0  # 高于基线

        # 凌晨 4 点应该最低
        early_morning = datetime(2026, 1, 1, 4, 0, 0, tzinfo=UTC)
        factor_4am = circadian.circadian_factor_for_cortisol(circadian.phase_at_time(early_morning))
        assert factor_4am < 0.3  # 低于基线

        # 8 点 > 4 点
        assert factor_8am > factor_4am


class TestUltradinRhythm:
    """90 分钟 Ultradian 周期测试"""

    def test_initialization(self):
        """测试初始化"""
        ultradian = UltradinRhythm(period_minutes=90)
        assert ultradian.phase == 0.0
        assert ultradian.period_minutes == 90

    def test_phase_update(self):
        """测试相位更新"""
        ultradian = UltradinRhythm(period_minutes=90)
        period_seconds = 90 * 60

        # 更新 1/4 周期
        ultradian.update(period_seconds / 4)
        assert abs(ultradian.phase - 0.25) < 0.01

        # 更新 1/4 周期，到达 1/2 周期
        ultradian.update(period_seconds / 4)
        assert abs(ultradian.phase - 0.5) < 0.01

    def test_fatigue_curve(self):
        """测试疲劳曲线"""
        ultradian = UltradinRhythm(period_minutes=90)

        # 开始（phase = 0）：疲劳 = 0
        fatigue = ultradian.fatigue_from_phase()
        assert abs(fatigue) < 0.01

        # 中点（phase = 0.5）：疲劳 = 1（最大）
        ultradian.phase = 0.5
        fatigue = ultradian.fatigue_from_phase()
        assert abs(fatigue - 1.0) < 0.01

        # 结束（phase ≈ 1）：疲劳 ≈ 0
        ultradian.phase = 0.99
        fatigue = ultradian.fatigue_from_phase()
        assert abs(fatigue) < 0.05


class TestHormoneDecay:
    """激素衰减模型测试"""

    def test_hormone_decay_exponential(self):
        """测试单指数衰减"""
        current = 1.0
        baseline = 0.5
        halflife = 60  # 1 分钟

        # 经过半衰期，应该衰减到一半
        decayed = BiologicalState._hormone_decay(current, baseline, halflife, dt=halflife)

        # C(t) = C0 * e^(-λt) + B(1-e^(-λt))
        # 在 t = halflife 时，e^(-λt) = 0.5
        # 所以 C = 1.0 * 0.5 + 0.5 * 0.5 = 0.75
        assert abs(decayed - 0.75) < 0.01

    def test_hormone_clipping(self):
        """测试激素浓度钳制 [0, 1]"""
        # 超过 1 时应该被截断
        clamped = BiologicalState._hormone_decay(current=2.0, baseline=0.5, halflife=60, dt=0.1)
        assert clamped <= 1.0

        # 低于 0 时应该被截断
        clamped = BiologicalState._hormone_decay(current=0.0, baseline=0.5, halflife=60, dt=1000)
        assert clamped >= 0.0


class TestBiologicalState:
    """生物状态测试"""

    def test_initialization(self):
        """测试初始化"""
        clock = ManualClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)

        assert bio.energy == 0.5
        assert bio.social_deficit == 0.3
        assert bio.novelty_craving == 0.4
        assert bio.hormones.dopamine == 0.5
        assert bio.hormones.cortisol == 0.4

    def test_update_energy_decay(self):
        """测试能量自然衰减"""
        clock = ManualClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        bio = BiologicalState(energy=1.0, clock=clock)

        initial_energy = bio.energy
        bio.update(dt=1.0)  # 更新 1 秒

        # 由于空闲衰减和 Ultradian 衰减，能量应该下降
        assert bio.energy < initial_energy

    def test_allostatic_load_calculation(self):
        """测试稳态超载计算"""
        clock = ManualClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        bio = BiologicalState(
            energy=0.1,
            hormones=HormoneState(dopamine=0.5, cortisol=0.9, serotonin=0.2, noradrenaline=0.4),
            clock=clock,
        )

        bio.update(dt=0.1)
        # 低能量 + 高皮质醇 + 低血清素 = 高压力指数
        assert bio.allostatic_load > 0.5

    def test_allostatic_load_weights_without_sleep_debt(self):
        """睡眠债删掉后，三项按 3 : 3 : 2 重新归一（D52 第 4 条）：全满是 1，全空是 0。"""
        clock = ManualClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)
        bio.energy = 0.0
        bio.hormones = HormoneState(cortisol=1.0, serotonin=0.0)
        bio._compute_allostatic_load()
        assert bio.allostatic_load == pytest.approx(1.0)
        bio.energy = 1.0
        bio.hormones = HormoneState(cortisol=0.0, serotonin=1.0)
        bio._compute_allostatic_load()
        assert bio.allostatic_load == pytest.approx(0.0)
        bio.energy = 0.5
        bio.hormones = HormoneState(cortisol=0.4, serotonin=0.5)
        bio._compute_allostatic_load()
        assert bio.allostatic_load == pytest.approx(0.5 * 0.375 + 0.4 * 0.375 + 0.5 * 0.25)


class TestEmotionalState:
    """情绪状态测试"""

    def test_valence_computation(self):
        """测试效价计算"""
        hormones = HormoneState(dopamine=0.8, cortisol=0.2, serotonin=0.8, noradrenaline=0.7)
        emotion = EmotionalState(hormones=hormones, energy=0.8)

        valence = emotion.compute_valence()

        # 高多巴胺 + 高血清素 + 低皮质醇 + 高去甲肾上腺素 = 高效价（正面）
        assert valence > 0.5

    def test_arousal_computation(self):
        """测试觉醒度计算"""
        hormones = HormoneState(dopamine=0.8, cortisol=0.2, serotonin=0.8, noradrenaline=0.8)
        emotion = EmotionalState(hormones=hormones, energy=0.9)

        arousal = emotion.compute_arousal()

        # 高多巴胺 + 高去甲肾上腺素 + 高能量 = 高觉醒度
        assert arousal > 0.6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
