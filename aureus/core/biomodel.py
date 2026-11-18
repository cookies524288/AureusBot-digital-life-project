"""生物学状态模型 - Aureus Phase 0.5 核心

实现激素系统、日周期、情绪派生、稳态超载。精力、社交需求、新奇感和 90 分钟节律
已经删掉（D53、D56）：精力现在来自真实的预算（见 aureus/body/）。
所有参数基于真实的神经生理学文献。

参考：docs/17-neuroscience-optimization.md
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from aureus.common.clock import Clock, SystemClock, ensure_aware

if TYPE_CHECKING:
    from aureus.body.energy import EnergyReading
    from aureus.core.cognitive_layer import CognitiveState

SECONDS_PER_DAY = 86400.0

# update() 把一段时间切成不超过这么多秒的小段，每小段用精确解推进，
# 所以结果和 update 被调用得勤不勤无关（D42 第 1 条）。
MAX_SEGMENT_SECONDS = 60.0
# 小段数的上限：停机几天后醒来，一次 update 不会算几千几万段；超过时每段相应变长。
MAX_SEGMENTS = 2000

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
class BiologicalState:
    """完整的生物学状态：激素、日周期、压力指数。
    这是 Aureus 核心内部状态的数学表达。
    """

    # 激素状态
    hormones: HormoneState = field(default_factory=HormoneState)

    # 稳态超载（压力指数）[0, 1]
    allostatic_load: float = 0.0

    # 时间信息
    clock: Clock = field(default_factory=SystemClock)
    # 日周期按哪个时区的昼夜算；不给就用这台电脑设置的时区（D42 第 3 条）。
    timezone: tzinfo | None = None
    circadian_phase: float = 0.0  # 日周期相位 [0, 1]

    # 上次更新时间（用于计算时间差）
    _last_update: datetime = field(init=False, default=None)  # type: ignore

    # 激素半衰期（秒）
    _halflife_dopamine: float = 90  # 1.5 分钟
    _halflife_cortisol: float = 3600  # 1 小时
    _halflife_serotonin: float = 86400  # 24 小时
    _halflife_noradrenaline: float = 300  # 5 分钟

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

    def _compute_allostatic_load(self) -> None:
        """计算稳态超载指数（多维压力累积）。

        原来还有精力枯竭和睡眠债两项：睡眠债没有来源已删（D52 第 4 条），精力现在是预算而不是
        模拟的量（D56），也不再混进激素的压力指数。剩下两项按 3 : 2 归一，满载时仍然是 1。
        """
        components = [
            self.hormones.cortisol * 0.6,  # 皮质醇高
            (1.0 - self.hormones.serotonin) * 0.4,  # 血清素低
        ]
        self.allostatic_load = min(1.0, sum(components))

    def compute_emotion(self) -> "EmotionalState":
        """计算当前情绪状态"""
        return EmotionalState(self.hormones)

    def observe_cognitive_state(
        self,
        context: dict[str, Any] | None = None,
        energy: "EnergyReading | None" = None,
    ) -> "CognitiveState":  # type: ignore
        """生成自己的认知观察（元认知，实现"我思故我在"）

        这是 D34 的实现：它能观察自己的思考过程。

        Args:
            energy: 身体的精力读数；不给时当作「不限」。
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
        return observer.observe(self, context or {}, energy)


@dataclass
class EmotionalState:
    """基于激素组合的情绪派生模型（二维情绪空间）。
    不是 LLM 编造的，而是激素浓度的直接映射。
    """

    hormones: HormoneState

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
        arousal = self.hormones.dopamine * 0.4 + self.hormones.noradrenaline * 0.6
        return max(0.0, min(1.0, arousal))

    def system_prompt_modifier(self) -> str:
        """生成给 LLM 的系统提示词修饰语"""
        valence = self.compute_valence()
        arousal = self.compute_arousal()

        modifiers = []

        # 基于觉醒度
        if arousal > 0.7:
            modifiers.append("你情绪高涨、反应敏捷")
        elif arousal < 0.3:
            modifiers.append("你有些懒散、反应放松")

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
