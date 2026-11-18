"""生物学状态模型 - Aureus Phase 0.5 核心

实现激素系统、能量模型、日周期、情绪派生、稳态超载等。
所有参数基于真实的神经生理学文献。

参考：docs/17-neuroscience-optimization.md
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from aureus.common.clock import Clock, SystemClock, ensure_aware
from aureus.common.config import BiomodelConfig

if TYPE_CHECKING:
    from aureus.core.cognitive_layer import CognitiveState

SECONDS_PER_HOUR = 3600.0
SECONDS_PER_DAY = 86400.0

# update() 把一段时间切成不超过这么多秒的小段，每小段用精确解推进，
# 所以结果和 update 被调用得勤不勤无关（D42 第 1 条）。
MAX_SEGMENT_SECONDS = 60.0
# 小段数的上限：停机几天后醒来，一次 update 不会算几千几万段；超过时每段相应变长。
MAX_SEGMENTS = 2000
# 90 分钟节律最疲劳时的消耗：每个周期最多消耗 15% 的能量范围。
ULTRADIAN_DIP_SCALE = 0.15
# 可调参数的默认值只在 BiomodelConfig 里写一次（D43），这里照抄，不另写数字。
_DEFAULT_RATES = BiomodelConfig()


# 各激素的基线浓度：既是初始值，也是衰减要回归的值，认知透明层也用它算「相对基线的偏离」。
# 只在这里写一次，免得几处各写各的、改一处忘另一处（审查第 5 节第 7 条）。
DOPAMINE_BASELINE = 0.5
CORTISOL_BASELINE = 0.4
SEROTONIN_BASELINE = 0.5
NORADRENALINE_BASELINE = 0.4


@dataclass
class HormoneState:
    """激素浓度状态 [0, 1]"""

    dopamine: float = DOPAMINE_BASELINE  # 多巴胺：动力、期待、奖励
    cortisol: float = CORTISOL_BASELINE  # 皮质醇：压力、警惕（受日周期影响）
    serotonin: float = SEROTONIN_BASELINE  # 血清素：满足感、稳定性（长期累积）
    noradrenaline: float = NORADRENALINE_BASELINE  # 去甲肾上腺素：注意力、警觉性


@dataclass
class CircadianModel:
    """简化的人类昼夜节律模型。
    参考：Cole 等人的 Kronauer 模型简化版。
    """

    def phase_at_time(self, clock_time: datetime) -> float:
        """返回节律相位 [0, 1]
        0 = 午夜，0.5 = 中午，1 = 下一个午夜

        clock_time 要先换成本地时间再传进来：日周期跟着当地的昼夜走（D42 第 3 条）。
        """
        midnight = clock_time.replace(hour=0, minute=0, second=0, microsecond=0)
        return ((clock_time - midnight).total_seconds() / SECONDS_PER_DAY) % 1.0

    def circadian_factor_for_cortisol(self, phase: float) -> float:
        """皮质醇的日周期调制倍数 (0-2 范围)
        凌晨 4-8 点上升，下午衰减
        """
        # 分段线性插值（模仿真实的皮质醇日周期）
        cortisol_curve = {
            0.0: 0.3,  # 午夜
            0.17: 0.2,  # 凌晨 4 点（最低）
            0.25: 0.5,  # 凌晨 6 点
            0.33: 1.2,  # 早晨 8 点（高峰）
            0.5: 0.8,  # 中午
            0.58: 0.7,  # 下午 2 点
            0.83: 0.5,  # 傍晚 8 点
            0.92: 0.3,  # 夜间 10 点
            1.0: 0.3,  # 午夜
        }
        return self._interpolate(cortisol_curve, phase)

    def circadian_factor_for_melatonin(self, phase: float) -> float:
        """褪黑素的日周期调制倍数，实际取值范围 [-0.6, 1.0]。
        晚上 9-11 点上升，早晨下降
        """
        # 使用正弦波模拟褪黑素的平滑上升与下降
        return 0.2 + 0.8 * math.sin(2 * math.pi * (phase + 0.3))

    def circadian_factor_for_alertness(self, phase: float) -> float:
        """警觉性的日周期调制倍数，实际取值范围 [0.3, 0.9]。
        午夜最高，中午最低（相位 0 取余弦最大值）
        """
        return 0.6 + 0.3 * math.cos(2 * math.pi * phase)

    @staticmethod
    def _interpolate(curve: dict[float, float], phase: float) -> float:
        """分段线性插值"""
        sorted_keys = sorted(curve.keys())

        # 调用方给的相位在 [0, 1)，曲线从 0.0 排到 1.0，覆盖整个范围，不需要回绕。
        # 找到插值区间
        for i, key in enumerate(sorted_keys[:-1]):
            if key <= phase <= sorted_keys[i + 1]:
                k1, k2 = key, sorted_keys[i + 1]
                v1, v2 = curve[k1], curve[k2]
                # 线性插值
                t = (phase - k1) / (k2 - k1)
                return v1 + t * (v2 - v1)

        # 如果没找到区间（不应发生），返回最近的值
        return curve[min(sorted_keys, key=lambda k: abs(k - phase))]


@dataclass
class UltradinRhythm:
    """90-120 分钟的基础认知周期（Ultradian Rhythm）。
    对应脑脊液清除和葡萄糖消耗的周期。
    """

    period_minutes: int = 90  # 周期长度（分钟）
    phase: float = 0.0  # 当前相位 [0, 1)

    def update(self, dt: float) -> None:
        """更新相位（dt 以秒为单位）"""
        period_seconds = self.period_minutes * 60
        self.phase = (self.phase + dt / period_seconds) % 1.0

    def fatigue_from_phase(self) -> float:
        """一个周期内的疲劳曲线（0-1）
        相位 0 和 1（周期刚开始、刚结束）都是 0，相位 0.5（周期中点）最高。
        是正弦曲线的前半个波，周期结束后回到 0，下一个周期重新开始。
        """
        return math.sin(self.phase * math.pi)

    def energy_dip_amount(self) -> float:
        """即使什么都不做，能量也会自然消耗的量。
        基于 Ultradian 周期的自然疲劳。
        """
        return self.fatigue_from_phase() * ULTRADIAN_DIP_SCALE

    def mean_energy_dip_over(self, dt: float) -> float:
        """从现在起往后 dt 秒里，energy_dip_amount() 的平均值（不改变相位）。

        为什么取平均而不是取某一刻的值：停机很久后一次 update，每小段可能长达几小时，
        跨过好几个 90 分钟周期，只取一刻会随取样点忽高忽低（D42 第 1 条）。
        """
        if dt <= 0:
            return self.energy_dip_amount()
        cycles = dt / (self.period_minutes * 60)
        start = self.phase
        mean_fatigue = (_integral_of_fatigue(start + cycles) - _integral_of_fatigue(start)) / cycles
        return mean_fatigue * ULTRADIAN_DIP_SCALE


def _integral_of_fatigue(phase: float) -> float:
    """疲劳曲线 sin(π·相位) 从 0 积分到 phase（phase 可以超过 1，按周期接着算）。

    每个整周期的积分是 2/π；不满一周期的部分是 (1 - cos(π·余数)) / π。
    """
    whole_cycles = math.floor(phase)
    remainder = phase - whole_cycles
    return whole_cycles * 2.0 / math.pi + (1.0 - math.cos(math.pi * remainder)) / math.pi


@dataclass
class BiologicalState:
    """完整的生物学状态：激素、能量、社交需求、无聊感、压力指数。
    这是 Aureus 核心内部状态的数学表达。
    """

    # 激素状态
    hormones: HormoneState = field(default_factory=HormoneState)

    # 能量状态 [0, 1]
    energy: float = 0.5
    energy_baseline: float = 0.5  # 基线（休息状态下自动回复到这里）
    # 精力向 1 回升的速率 ρ（每小时）：dE/dt = ρ(1 - E) - 节律消耗（D42 第 4 条）。
    energy_recovery_rate: float = _DEFAULT_RATES.energy_recovery_rate
    energy_recovery_rate_sleep: float = 0.40  # 每小时恢复率（深度睡眠时），暂时没用到
    # 空闲消耗 energy_decay_idle 已去掉：每秒 0.002 约 4 分钟耗光精力，空闲消耗由节律给出。
    energy_decay_think: float = 0.01  # 每秒消耗（思考 / 调用 LLM 时）
    energy_decay_interact: float = 0.005  # 每秒消耗（社交互动时）

    # 睡眠债（小时）
    sleep_debt_hours: float = 0.0

    # 社交需求 [0, 1]：0 = 满足，1 = 极度孤独
    social_deficit: float = 0.3
    # 社交需求随时间上涨（04 号文档），不再自然衰减（D42 第 5、7 条）
    social_timeout_per_hour: float = _DEFAULT_RATES.social_timeout_per_hour  # 每小时增长
    # 每回复一次让它下降多少
    social_relief_per_conversation: float = _DEFAULT_RATES.social_relief_per_conversation

    # 无聊感 / 刺激饥渴 [0, 1]
    novelty_craving: float = 0.4
    # 新奇感也随时间上涨；原来的“每秒 0.0005”是单位弄混了（D42 第 6 条）
    novelty_growth_per_hour: float = _DEFAULT_RATES.novelty_growth_per_hour  # 每小时增长
    novelty_decay_on_novel: float = 0.15  # 每次新事件减少
    # 每回复一次减少多少（D42 第 7 条）
    novelty_relief_per_conversation: float = _DEFAULT_RATES.novelty_relief_per_conversation

    # 稳态超载（压力指数）[0, 1]
    allostatic_load: float = 0.0

    # 时间信息
    clock: Clock = field(default_factory=SystemClock)
    # 日周期按哪个时区的昼夜算；不给就用这台电脑设置的时区（D42 第 3 条）。
    timezone: tzinfo | None = None
    circadian_phase: float = 0.0  # 日周期相位 [0, 1]

    # Ultradian 周期
    ultradian: UltradinRhythm = field(default_factory=UltradinRhythm)

    # 上次更新时间（用于计算时间差）
    _last_update: datetime = field(init=False, default=None)  # type: ignore

    # 激素半衰期（秒）
    _halflife_dopamine: float = 90  # 1.5 分钟
    _halflife_cortisol: float = 3600  # 1 小时
    _halflife_serotonin: float = 86400  # 24 小时
    _halflife_noradrenaline: float = 300  # 5 分钟

    @classmethod
    def from_config(
        cls, config: BiomodelConfig, clock: Clock, timezone: tzinfo | None = None
    ) -> "BiologicalState":
        """按 aureus.toml 的 [biomodel] 建一个生物状态，其余字段用默认值（D43）。

        Args:
            config: 可调参数。
            clock: 时钟。
            timezone: 日周期按哪个时区算；不给时由心跳填成它的时区。
        """
        return cls(clock=clock, timezone=timezone, **config.model_dump())

    def __post_init__(self) -> None:
        """初始化时间戳"""
        self._last_update = self.clock.now()

    def update(self, dt: float, now: datetime | None = None) -> None:
        """让生物状态经过 dt 秒（D42 第 1 条）。

        内部把这段时间切成不超过 MAX_SEGMENT_SECONDS 秒的小段，每小段用精确解推进，
        所以每秒更新一次、每分钟一次、每小时一次，结果一致。

        Args:
            dt: 经过的时间（秒），不能是负数。
            now: 这段时间结束的时刻；不给就取时钟现在的时间。

        Raises:
            ValueError: dt 是负数，或者不是有限的数。
        """
        if not math.isfinite(dt) or dt < 0:
            # 为什么报错而不是当成 0：时钟往回调时怎么办由调用方（心跳）决定，
            # 这里悄悄吞掉，激素和精力就停着不动，却没有人知道。
            raise ValueError(f"经过的时间 dt 必须是不小于 0 的有限数，但收到了 {dt}")
        end = ensure_aware(now) if now is not None else self.clock.now()
        start = end - timedelta(seconds=dt)
        segments = min(MAX_SEGMENTS, max(1, math.ceil(dt / MAX_SEGMENT_SECONDS)))
        step = dt / segments
        circadian_model = CircadianModel()
        for index in range(segments):
            middle = start + timedelta(seconds=(index + 0.5) * step)
            phase = circadian_model.phase_at_time(middle.astimezone(self.timezone))
            self._advance_segment(step, phase)
        self.circadian_phase = circadian_model.phase_at_time(end.astimezone(self.timezone))
        self._compute_allostatic_load()
        self._last_update = end

    def _advance_segment(self, dt: float, circadian_phase: float) -> None:
        """推进一小段。日周期相位由调用方按这一小段中点的时间算好传进来。"""
        self._update_hormones(dt, circadian_phase)
        self._update_energy(dt)
        self.ultradian.update(dt)
        self._update_social_deficit(dt)
        self._update_novelty_craving(dt)

    def _update_hormones(self, dt: float, circadian_phase: float) -> None:
        """更新激素浓度（指数衰减 + 基线恢复）。皮质醇的基线随日周期变化。"""
        # 多巴胺
        self.hormones.dopamine = self._hormone_decay(
            current=self.hormones.dopamine,
            baseline=DOPAMINE_BASELINE,
            halflife=self._halflife_dopamine,
            dt=dt,
        )

        # 皮质醇：向“基线 × 日周期系数”回归。日周期调制的是基线，不是算出来的结果——
        # 原来每更新一次就把结果乘一遍系数，更新得越勤结果越偏（D42 第 2 条）。
        cortisol_circadian_factor = CircadianModel().circadian_factor_for_cortisol(circadian_phase)
        self.hormones.cortisol = self._hormone_decay(
            current=self.hormones.cortisol,
            baseline=CORTISOL_BASELINE * cortisol_circadian_factor,
            halflife=self._halflife_cortisol,
            dt=dt,
        )

        # 血清素
        self.hormones.serotonin = self._hormone_decay(
            current=self.hormones.serotonin,
            baseline=SEROTONIN_BASELINE,
            halflife=self._halflife_serotonin,
            dt=dt,
        )

        # 去甲肾上腺素
        self.hormones.noradrenaline = self._hormone_decay(
            current=self.hormones.noradrenaline,
            baseline=NORADRENALINE_BASELINE,
            halflife=self._halflife_noradrenaline,
            dt=dt,
        )

    @staticmethod
    def _hormone_decay(
        current: float,
        baseline: float,
        halflife: float,
        dt: float,
    ) -> float:
        """单指数衰减模型：C(t) = C₀·e^(-λt) + baseline·(1 - e^(-λt))

        current: 当前浓度
        baseline: 基线浓度
        halflife: 半衰期（秒）
        dt: 时间增量（秒）
        baseline 在这 dt 秒里当作不变；基线随时间变化时，由 update() 切成小段来推进。
        """
        decay_rate = 0.693147 / halflife  # ln(2)
        decayed = current * math.exp(-decay_rate * dt)
        recovered_baseline = baseline * (1 - math.exp(-decay_rate * dt))
        result = decayed + recovered_baseline
        return max(0.0, min(1.0, result))  # 钳制在 [0, 1]

    def _update_energy(self, dt: float) -> None:
        """更新精力：dE/dt = ρ(1 - E) - 节律消耗，在这 dt 秒里取精确解（D42 第 4 条）。

        节律消耗取这 dt 秒里的平均值，所以要在 90 分钟节律往前走之前调用。
        """
        recovery_per_second = self.energy_recovery_rate / SECONDS_PER_HOUR
        period_seconds = self.ultradian.period_minutes * 60
        drain_per_second = self.ultradian.mean_energy_dip_over(dt) / period_seconds
        # 令 x = 1 - E（离满精力还差多少），则 dx/dt = -ρx + m，
        # 解是 x(dt) = x·e^(-ρ·dt) + (m/ρ)·(1 - e^(-ρ·dt))。
        if recovery_per_second > 0:
            shrink = math.exp(-recovery_per_second * dt)
            carried = (1.0 - self.energy) * shrink
            produced = drain_per_second / recovery_per_second * (1.0 - shrink)
            shortfall = carried + produced
        else:
            shortfall = (1.0 - self.energy) + drain_per_second * dt
        self.energy = max(0.0, min(1.0, 1.0 - shortfall))

    def _update_social_deficit(self, dt: float) -> None:
        """更新社交需求：随时间上涨，涨到 1 为止（04 号文档，D42 第 5 条）。"""
        growth = self.social_timeout_per_hour * dt / SECONDS_PER_HOUR
        self.social_deficit = min(1.0, self.social_deficit + growth)

    def _update_novelty_craving(self, dt: float) -> None:
        """更新无聊感：随时间上涨，涨到 1 为止（D42 第 6 条）。"""
        growth = self.novelty_growth_per_hour * dt / SECONDS_PER_HOUR
        self.novelty_craving = min(1.0, self.novelty_craving + growth)

    def _compute_allostatic_load(self) -> None:
        """计算稳态超载指数（多维压力累积）"""
        components = [
            (1.0 - self.energy) * 0.3,  # 能量枯竭 30%
            self.hormones.cortisol * 0.3,  # 皮质醇高 30%
            (1.0 - self.hormones.serotonin) * 0.2,  # 血清素低 20%
            self.sleep_debt_hours / 7.0 * 0.2,  # 睡眠债 20%
        ]
        self.allostatic_load = min(1.0, sum(components))

    # ==================== 激素事件触发器 ====================

    def trigger_dopamine_boost(self, amount: float, reason: str = "") -> None:
        """触发多巴胺上升（积极预测误差、目标达成）"""
        self.hormones.dopamine = min(1.0, self.hormones.dopamine + amount)

    def trigger_dopamine_drop(self, amount: float, reason: str = "") -> None:
        """触发多巴胺下降（消极预测误差、期望落空）"""
        self.hormones.dopamine = max(0.0, self.hormones.dopamine - amount)

    def trigger_cortisol_spike(self, amount: float, reason: str = "") -> None:
        """触发皮质醇上升（应激事件）"""
        self.hormones.cortisol = min(1.0, self.hormones.cortisol + amount)

    def trigger_serotonin_boost(self, amount: float, reason: str = "") -> None:
        """触发血清素上升（积极社交、成就感）"""
        self.hormones.serotonin = min(1.0, self.hormones.serotonin + amount)

    def trigger_serotonin_drop(self, amount: float, reason: str = "") -> None:
        """触发血清素下降（拒绝、失败、孤独）"""
        self.hormones.serotonin = max(0.0, self.hormones.serotonin - amount)

    def trigger_noradrenaline_spike(self, amount: float, reason: str = "") -> None:
        """触发去甲肾上腺素上升（新奇刺激、紧急消息）"""
        self.hormones.noradrenaline = min(1.0, self.hormones.noradrenaline + amount)

    def on_novel_event(self) -> None:
        """处理新事件的影响"""
        self.novelty_craving = max(0.0, self.novelty_craving - self.novelty_decay_on_novel)
        self.trigger_noradrenaline_spike(0.3, "novel_event")

    def on_conversation(self) -> None:
        """回复了一次之后：社交需求和新奇感回落（04 号文档：一次有效对话后回落，D42 第 7 条）。

        心跳每把一条回复交给用户就调用一次，所以回落量按“每次回复”定（D43）。

        只动这两个量，不动激素：激素由真实事件触发是 Phase 2 反馈的事（D20）。
        """
        self.social_deficit = max(0.0, self.social_deficit - self.social_relief_per_conversation)
        self.novelty_craving = max(0.0, self.novelty_craving - self.novelty_relief_per_conversation)

    def on_positive_social_interaction(self) -> None:
        """处理积极社交互动（0.5 留下的事件，还没有调用方；和每次回复的回落分开，见 D43）"""
        self.social_deficit = max(0.0, self.social_deficit - 0.1)
        self.trigger_serotonin_boost(0.05, "positive_social")
        self.trigger_dopamine_boost(0.2, "positive_social")

    def on_social_rejection(self) -> None:
        """处理社交拒绝"""
        self.social_deficit = min(1.0, self.social_deficit + 0.15)
        self.trigger_serotonin_drop(0.08, "rejection")
        self.trigger_cortisol_spike(0.2, "rejection")

    def on_error_or_failure(self) -> None:
        """处理错误或失败事件"""
        self.trigger_dopamine_drop(0.1, "failure")
        self.trigger_cortisol_spike(0.2, "error")

    def on_goal_achieved(self) -> None:
        """处理目标达成"""
        self.trigger_dopamine_boost(0.4, "goal_achieved")
        self.trigger_cortisol_spike(-0.1, "success_relief")  # 负值 = 降低

    def consume_energy_for_thinking(self, duration_seconds: float) -> bool:
        """消耗思考能量（调用 LLM）。

        返回：是否有足够的能量
        """
        required = self.energy_decay_think * duration_seconds
        if self.energy >= required:
            self.energy = max(0.0, self.energy - required)
            return True
        return False

    def consume_energy_for_interaction(self, duration_seconds: float) -> bool:
        """消耗社交互动能量。

        返回：是否有足够的能量
        """
        required = self.energy_decay_interact * duration_seconds
        if self.energy >= required:
            self.energy = max(0.0, self.energy - required)
            return True
        return False

    # ==================== 行为约束生成 ====================

    def generate_behavioral_constraints(self) -> dict[str, Any]:
        """生成给规划器的约束条件。
        基于当前激素、能量、情绪状态。
        """
        emotional_state = self.compute_emotion()
        arousal = emotional_state.compute_arousal()

        return {
            "max_planning_depth": self.planning_depth(arousal),
            "working_memory_limit": self.working_memory_items(),
            "priority_adjustments": self.priority_modifiers(),
            "risk_aversion_factor": self.risk_aversion(),
            "social_motivation_boost": self.social_motivation(),
            "mood_description": emotional_state.mood_text(),
            "can_think": self.energy > 0.3,
            "must_rest": self.energy < 0.1,
            "burnout_risk": AllostasisModel.burnout_risk(self.allostatic_load),
        }

    def compute_emotion(self) -> "EmotionalState":
        """计算当前情绪状态"""
        return EmotionalState(self.hormones, self.energy)

    def planning_depth(self, arousal: float) -> int:
        """规划器能考虑多少步（3-7 步）"""
        depth = 3 + int(arousal * 4)
        if self.energy < 0.3:
            depth = max(1, int(depth * 0.5))
        return depth

    def working_memory_items(self) -> int:
        """工作记忆容量（Miller's 7±2）"""
        base = 7
        variance = int(self.hormones.noradrenaline * 4) - 2
        return max(4, min(9, base + variance))

    def priority_modifiers(self) -> dict[str, float]:
        """不同目标类型的优先级调整倍数"""
        modifiers = {}

        # 能量低时，只有高优先级任务能执行
        if self.energy < 0.3:
            modifiers["low_priority"] = 0.3
            modifiers["maintenance"] = 2.0
            modifiers["rest"] = 3.0

        # 皮质醇高时，倾向于安全的、已知的任务
        if self.hormones.cortisol > 0.8:
            modifiers["novel_exploration"] = 0.1
            modifiers["risk_taking"] = 0.1
            modifiers["maintenance"] = 2.0
            modifiers["rest"] = 2.0

        # 血清素低时，消极滤镜
        if self.hormones.serotonin < 0.3:
            modifiers["help_others"] = 0.5
            modifiers["long_term_project"] = 0.5
            modifiers["self_protection"] = 1.5
            modifiers["avoid_harm"] = 1.5

        # 社交需求高时，社交任务权重上升
        if self.social_deficit > 0.6:
            modifiers["social_interaction"] = 1.8

        # 无聊时，新奇任务权重上升
        if self.novelty_craving > 0.6:
            modifiers["novel_exploration"] = 1.5

        return modifiers

    def risk_aversion(self) -> float:
        """风险规避程度 [0, 1]，越高越保守"""
        return self.hormones.cortisol * 0.6 + (1.0 - self.hormones.dopamine) * 0.4

    def social_motivation(self) -> float:
        """社交动机 [0, 1]"""
        return (
            self.social_deficit * 0.5 + self.hormones.dopamine * 0.3 + self.hormones.serotonin * 0.2
        )

    def observe_cognitive_state(self, context: dict[str, Any] | None = None) -> "CognitiveState":  # type: ignore
        """生成自己的认知观察（元认知，实现"我思故我在"）

        这是 D34 的实现：它能观察自己的思考过程。

        Args:
            context: 当前执行上下文，包含：
                - task_name: 当前任务名
                - planning_depth: 规划深度
                - focus_target: 注意力焦点

        Returns:
            CognitiveState：对当前思考状态的观察

        注意：需要导入 CognitiveObserver，但为了避免循环导入，
        这里延迟导入。实际使用时应该这样：

        ```python
        from aureus.core.cognitive_layer import CognitiveObserver
        observer = CognitiveObserver(self.clock)
        cognitive = observer.observe(self, context)
        ```
        """
        from aureus.core.cognitive_layer import CognitiveObserver

        observer = CognitiveObserver(self.clock)
        return observer.observe(self, context or {})


@dataclass
class EmotionalState:
    """基于激素组合的情绪派生模型（二维情绪空间）。
    不是 LLM 编造的，而是激素浓度的直接映射。
    """

    hormones: HormoneState
    energy: float

    def compute_valence(self) -> float:
        """情绪效价轴 [-1, 1]
        正数：积极（喜悦、满足）
        负数：消极（悲伤、焦虑）
        """
        valence = (
            self.hormones.dopamine * 0.4  # 多巴胺 → 期待和动力
            + self.hormones.serotonin * 0.4  # 血清素 → 满足感和稳定
            - self.hormones.cortisol * 0.3  # 皮质醇 → 压力
            - (1.0 - self.hormones.noradrenaline) * 0.1  # 去甲肾上腺素低 → 消极
        )
        return max(-1.0, min(1.0, valence))

    def compute_arousal(self) -> float:
        """觉醒度轴 [0, 1]
        高：激活、警觉、兴奋
        低：放松、困倦、无聊
        """
        arousal = (
            self.hormones.dopamine * 0.4 + self.hormones.noradrenaline * 0.6 + self.energy * 0.3
        ) / 1.3  # 标准化
        return max(0.0, min(1.0, arousal))

    def emotion_label(self) -> str:
        """从 2D 点映射到情绪标签。
        基于 Russell 的环状情绪模型。
        """
        valence = self.compute_valence()
        arousal = self.compute_arousal()

        # 象限划分
        if valence > 0.3 and arousal > 0.5:
            return "joy" if arousal > 0.7 else "contentment"
        elif valence < -0.3 and arousal > 0.5:
            return "anxiety" if arousal > 0.7 else "irritation"
        elif valence > 0.3 and arousal < 0.5:
            return "serenity"
        elif valence < -0.3 and arousal < 0.5:
            return "sadness" if arousal < 0.3 else "apathy"
        else:
            return "neutral"

    def mood_text(self) -> str:
        """生成人类可读的心情描述"""
        label = self.emotion_label()

        mood_map = {
            "joy": "我感到很开心，充满活力！",
            "contentment": "我感到满足和舒适。",
            "serenity": "我感到平静和放松。",
            "anxiety": "我感到焦虑和不安。",
            "irritation": "我感到有些烦躁。",
            "sadness": "我感到有些低落。",
            "apathy": "我感到有点冷漠和疲惫。",
            "neutral": "我感觉还好。",
        }

        return mood_map.get(label, "我的心情复杂。")

    def system_prompt_modifier(self) -> str:
        """生成给 LLM 的系统提示词修饰语"""
        valence = self.compute_valence()
        arousal = self.compute_arousal()

        modifiers = []

        # 基于觉醒度
        if arousal > 0.7:
            modifiers.append("你精力充沛、充满热情")
        elif arousal < 0.3:
            modifiers.append("你感到困倦、反应变慢")

        # 基于效价
        if valence > 0.5:
            modifiers.append("你心情愉快、积极乐观")
        elif valence < -0.5:
            modifiers.append("你心情低落、倾向消极")

        # 特殊状态
        if self.hormones.cortisol > 0.8:
            modifiers.append("你感到紧张、谨慎小心")
        if self.hormones.dopamine > 0.7:
            modifiers.append("你充满动力、渴望行动")
        if self.hormones.serotonin < 0.3:
            modifiers.append("你缺乏安全感、需要社交支持")

        return "；".join(modifiers) + "。" if modifiers else ""


class AllostasisModel:
    """稳态超载（Allostatic Load）模型。
    长期高皮质醇、低血清素、能量枯竭的组合 → 倦怠、抑郁。
    """

    @staticmethod
    def burnout_risk(allostatic_load: float) -> dict[str, Any]:
        """返回倦怠风险评估。

        allostatic_load: 稳态超载指数 [0, 1]
        """
        risk_level = (
            "critical"
            if allostatic_load > 0.7
            else "high"
            if allostatic_load > 0.5
            else "moderate"
            if allostatic_load > 0.3
            else "low"
        )

        days_until_collapse = (1.0 - allostatic_load) * 7.0  # 粗略估计

        recommended_action = (
            "Force sleep immediately"
            if allostatic_load > 0.8
            else "Reduce workload"
            if allostatic_load > 0.6
            else "Maintain current pace"
            if allostatic_load > 0.4
            else "Can handle more tasks"
        )

        return {
            "risk_level": risk_level,
            "days_until_collapse": days_until_collapse,
            "recommended_action": recommended_action,
        }
