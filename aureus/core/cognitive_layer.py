"""认知透明层 - 让它能观察自己的思考过程

实现"我思故我在"：通过自我观察元认知，它能知道自己在思考什么、为什么这样想。

参考：D34, docs/18-neuroscience-completeness.md
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aureus.common.clock import Clock, SystemClock
from aureus.core.biomodel import (
    CORTISOL_BASELINE,
    DOPAMINE_BASELINE,
    NORADRENALINE_BASELINE,
    SEROTONIN_BASELINE,
)
from aureus.feedback import FeedbackError

if TYPE_CHECKING:
    from aureus.body.energy import EnergyReading
    from aureus.core.biomodel import BiologicalState
    from aureus.feedback import FeedbackStore

# 没有任何反馈历史时，信心栏显示的话（D67：不装作有把握）。
_NO_CONFIDENCE_DATA = "没有数据"

# 精力档位的显示名；这里只认值，不 import body（core 在 body 之上，运行时不绕回去）。
_TIER_UNLIMITED = "不限"
_TIER_SAVING = "节省"
_TIER_LOW = "低电量"
_TIER_OVERDRAFT = "透支保护"


@dataclass
class CognitiveState:
    """它对自己当前认知状态的观察（元认知）"""

    # 当前焦点
    current_task: str = ""  # "回复用户" / "计划今天" / "思考问题" / "待机"
    planning_depth: int = 0  # 正在规划第几步 [0, 7]
    # 对当前决策的信心 [0, 1]：来自反馈里该类事情最近的真实成功率（D67）。
    # None 表示没有数据（没有反馈存储、没指定类别，或这一类还没有有结果的历史）。
    planning_confidence: float | None = None
    confidence_note: str = _NO_CONFIDENCE_DATA  # 给人看的说明，含类别和样本数

    # 内部状态对决策的影响
    active_hormones: dict[str, float] = field(
        default_factory=lambda: {
            "dopamine_effect": 0.0,
            "cortisol_effect": 0.0,
            "serotonin_effect": 0.0,
            "noradrenaline_effect": 0.0,
        }
    )  # 相对于基线的激素影响

    # 精力来自身体信号（预算），不是模拟的量（D53、D56）：档位名，和精力比例 [0, 1]；
    # 没设月度上限（不限）时比例是 None。
    energy_state: str = _TIER_UNLIMITED
    energy_level: float | None = None

    attention_focus: str = ""  # 注意力聚焦在哪：用户名、任务、想法等

    # 内省：为什么做这个决定？
    why_this_decision: str = ""  # 决策理由（自然语言）
    obstacles: list[str] = field(default_factory=list)  # ["能量不足", "皮质醇高，谨慎", ...]
    next_likely_action: str | None = None  # 预测下一步做什么

    # 时间戳
    last_updated: datetime | None = None

    def self_awareness_statement(self) -> str:
        """生成"我思故我在"的自然语言陈述"""
        if not self.current_task:
            return "我现在待机中。"

        parts = [f"我现在在{self.current_task}"]

        if self.planning_depth > 0:
            parts.append(f"，正规划第{self.planning_depth}步")

        if self.why_this_decision:
            parts.append(f"，{self.why_this_decision}")

        if self.obstacles:
            parts.append(f"。但有障碍：{' / '.join(self.obstacles)}")
        else:
            parts.append("，没有明显障碍")

        if self.next_likely_action:
            parts.append(f"。接下来可能会{self.next_likely_action}")

        return "".join(parts) + "。"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于序列化和时间线存储）"""
        return {
            "current_task": self.current_task,
            "planning_depth": self.planning_depth,
            "planning_confidence": self.planning_confidence,
            "confidence_note": self.confidence_note,
            "active_hormones": self.active_hormones,
            "energy_state": self.energy_state,
            "energy_level": self.energy_level,
            "attention_focus": self.attention_focus,
            "why_this_decision": self.why_this_decision,
            "obstacles": self.obstacles,
            "next_likely_action": self.next_likely_action,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }


class CognitiveObserver:
    """观察器：让它能观察自己的思考过程；精力读身体信号，激素读生物状态，信心读反馈。"""

    def __init__(self, clock: Clock | None = None, feedback: FeedbackStore | None = None):
        self.clock = clock or SystemClock()
        # 不给反馈存储（或 [feedback] 关闭）时，信心一律显示没有数据。
        self._feedback = feedback

    def observe(
        self,
        bio_state: BiologicalState,  # type: ignore # 避免循环导入
        context: dict[str, Any] | None = None,
        energy: EnergyReading | None = None,
    ) -> CognitiveState:
        """根据生物状态、身体信号和当前上下文，生成它对自己的观察

        Args:
            bio_state: 生物学状态对象
            context: 当前执行上下文（任务名、规划深度、反馈类别 feedback_category 等）
            energy: 身体的精力读数；不给时当作「不限」。

        Returns:
            CognitiveState：对当前认知状态的描述
        """
        context = context or {}

        # 1. 识别当前任务
        current_task = context.get("task_name", "待机")

        # 2. 规划深度（规划器提供）
        planning_depth = context.get("planning_depth", 0)

        # 3. 规划信心：读反馈里这一类事情的真实成功率，不再用激素拼（D67）
        planning_confidence, confidence_note = self._read_confidence(
            context.get("feedback_category")
        )

        # 4. 激素效应（相对于基线）
        active_hormones = {
            "dopamine_effect": bio_state.hormones.dopamine - DOPAMINE_BASELINE,
            "cortisol_effect": bio_state.hormones.cortisol - CORTISOL_BASELINE,
            "serotonin_effect": bio_state.hormones.serotonin - SEROTONIN_BASELINE,
            "noradrenaline_effect": bio_state.hormones.noradrenaline - NORADRENALINE_BASELINE,
        }

        # 5. 精力：直接用身体给的档位，不在这里另算
        energy_state = energy.tier.value if energy is not None else _TIER_UNLIMITED
        energy_level = energy.fraction if energy is not None else None

        # 6. 注意力焦点
        attention_focus = context.get("focus_target", "")

        # 7. 决策理由
        why_this_decision = self._explain_current_state(bio_state, energy_state)

        # 8. 障碍识别
        obstacles = self._identify_obstacles(bio_state, energy_state)

        # 9. 下一步预测
        next_likely_action = self._predict_next_action(bio_state, current_task, energy_state)

        return CognitiveState(
            current_task=current_task,
            planning_depth=planning_depth,
            planning_confidence=planning_confidence,
            confidence_note=confidence_note,
            active_hormones=active_hormones,
            energy_state=energy_state,
            energy_level=energy_level,
            attention_focus=attention_focus,
            why_this_decision=why_this_decision,
            obstacles=obstacles,
            next_likely_action=next_likely_action,
            last_updated=self.clock.now(),
        )

    def _read_confidence(self, category: str | None) -> tuple[float | None, str]:
        """从反馈取这一类事情的自信；取不到就如实说没有数据。

        反馈读失败时朝安全方向倒：当作没有数据，不让观察这一步拖垮心跳。
        """
        if self._feedback is None or not category:
            return None, _NO_CONFIDENCE_DATA
        try:
            confidence = self._feedback.confidence(category)
        except (FeedbackError, sqlite3.Error):
            return None, _NO_CONFIDENCE_DATA
        return confidence.value, confidence.describe()

    @staticmethod
    def _explain_current_state(bio_state: BiologicalState, energy_state: str) -> str:  # type: ignore
        """根据激素和能量，解释为什么做当前这件事"""
        reasons = []

        # 多巴胺
        if bio_state.hormones.dopamine > 0.7:
            reasons.append("多巴胺充足，充满动力")
        elif bio_state.hormones.dopamine < 0.3:
            reasons.append("多巴胺低落，比较被动")

        # 精力：今天的额度不多了
        if energy_state in (_TIER_SAVING, _TIER_LOW, _TIER_OVERDRAFT):
            reasons.append(f"今天的精力额度紧张（{energy_state}档）")

        # 皮质醇
        if bio_state.hormones.cortisol > 0.8:
            reasons.append("皮质醇高，保持谨慎")

        return "；".join(reasons) if reasons else "保持常态"

    @staticmethod
    def _identify_obstacles(bio_state: BiologicalState, energy_state: str) -> list[str]:  # type: ignore
        """识别当前行动的障碍"""
        obstacles = []

        if energy_state == _TIER_OVERDRAFT:
            obstacles.append("今天的额度几乎用完（透支保护）")
        elif energy_state == _TIER_LOW:
            obstacles.append("精力低电量")
        elif energy_state == _TIER_SAVING:
            obstacles.append("精力进入节省档")

        if bio_state.hormones.cortisol > 0.8:
            obstacles.append("皮质醇高（谨慎模式）")

        if bio_state.hormones.serotonin < 0.3:
            obstacles.append("血清素低（消极倾向）")

        if bio_state.allostatic_load > 0.7:
            obstacles.append("压力积累（需要休息）")

        return obstacles

    @staticmethod
    def _predict_next_action(
        bio_state: BiologicalState,  # type: ignore
        current_task: str,
        energy_state: str,
    ) -> str | None:
        """基于当前状态预测下一步可能的行动"""
        # 今天的额度快用完：只回应用户，其余留到明天
        if energy_state in (_TIER_LOW, _TIER_OVERDRAFT):
            return "只回应用户，其余的事留到明天"

        # 如果压力积累很高
        if bio_state.allostatic_load > 0.7:
            return "寻求恢复（休息、社交）"

        # 待机时：额度紧张就继续待着，否则找事做
        if current_task == "待机":
            if energy_state in (_TIER_SAVING, _TIER_LOW, _TIER_OVERDRAFT):
                return "继续待机"
            return "寻找新任务"

        return None

    @staticmethod
    def system_prompt_modifier(bio_state: BiologicalState) -> str:  # type: ignore
        """生成给 LLM 的系统提示词修饰语（基于当前认知状态）"""
        modifiers = []

        # 基于多巴胺
        if bio_state.hormones.dopamine > 0.7:
            modifiers.append("你充满动力、热情高涨")
        elif bio_state.hormones.dopamine < 0.3:
            modifiers.append("你感到无力、兴致不高")

        # 基于皮质醇
        if bio_state.hormones.cortisol > 0.8:
            modifiers.append("你感到紧张、需要谨慎")
        elif bio_state.hormones.cortisol < 0.2:
            modifiers.append("你感到放松、警惕性下降")

        # 基于血清素
        if bio_state.hormones.serotonin > 0.7:
            modifiers.append("你心情稳定、倾向乐观")
        elif bio_state.hormones.serotonin < 0.3:
            modifiers.append("你感到不稳定、倾向消极")

        if not modifiers:
            modifiers.append("你感到平静、状态正常")

        return "；".join(modifiers) + "。"
