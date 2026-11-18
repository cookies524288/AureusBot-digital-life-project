"""认知透明层 - 让它能观察自己的思考过程

实现"我思故我在"：通过自我观察元认知，它能知道自己在思考什么、为什么这样想。

参考：D34, docs/18-neuroscience-completeness.md
"""

from __future__ import annotations

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

if TYPE_CHECKING:
    from aureus.core.biomodel import BiologicalState


@dataclass
class CognitiveState:
    """它对自己当前认知状态的观察（元认知）"""

    # 当前焦点
    current_task: str = ""  # "回复用户" / "计划今天" / "思考问题" / "待机"
    planning_depth: int = 0  # 正在规划第几步 [0, 7]
    planning_confidence: float = 0.5  # [0, 1] 对当前决策的信心

    # 内部状态对决策的影响
    active_hormones: dict[str, float] = field(
        default_factory=lambda: {
            "dopamine_effect": 0.0,
            "cortisol_effect": 0.0,
            "serotonin_effect": 0.0,
            "noradrenaline_effect": 0.0,
        }
    )  # 相对于基线的激素影响

    energy_state: str = "normal"  # "充足" / "正常" / "疲倦" / "濒死"
    energy_level: float = 0.5  # [0, 1] 绝对能量值

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
    """观察器：让 BiologicalState 能观察自己的思考过程"""

    def __init__(self, clock: Clock | None = None):
        self.clock = clock or SystemClock()

    def observe(
        self,
        bio_state: BiologicalState,  # type: ignore # 避免循环导入
        context: dict[str, Any] | None = None,
    ) -> CognitiveState:
        """根据生物状态和当前上下文，生成它对自己的观察

        Args:
            bio_state: 生物学状态对象
            context: 当前执行上下文（任务名、规划深度等）

        Returns:
            CognitiveState：对当前认知状态的描述
        """
        context = context or {}

        # 1. 识别当前任务
        current_task = context.get("task_name", "待机")

        # 2. 规划深度（规划器提供）
        planning_depth = context.get("planning_depth", 0)

        # 3. 规划信心（基于多巴胺和能量）
        planning_confidence = self._compute_confidence(bio_state)

        # 4. 激素效应（相对于基线）
        active_hormones = {
            "dopamine_effect": bio_state.hormones.dopamine - DOPAMINE_BASELINE,
            "cortisol_effect": bio_state.hormones.cortisol - CORTISOL_BASELINE,
            "serotonin_effect": bio_state.hormones.serotonin - SEROTONIN_BASELINE,
            "noradrenaline_effect": bio_state.hormones.noradrenaline - NORADRENALINE_BASELINE,
        }

        # 5. 能量分类
        energy_state = self._classify_energy(bio_state.energy)
        energy_level = bio_state.energy

        # 6. 注意力焦点
        attention_focus = context.get("focus_target", "")

        # 7. 决策理由
        why_this_decision = self._explain_current_state(bio_state)

        # 8. 障碍识别
        obstacles = self._identify_obstacles(bio_state)

        # 9. 下一步预测
        next_likely_action = self._predict_next_action(bio_state, current_task)

        return CognitiveState(
            current_task=current_task,
            planning_depth=planning_depth,
            planning_confidence=planning_confidence,
            active_hormones=active_hormones,
            energy_state=energy_state,
            energy_level=energy_level,
            attention_focus=attention_focus,
            why_this_decision=why_this_decision,
            obstacles=obstacles,
            next_likely_action=next_likely_action,
            last_updated=self.clock.now(),
        )

    @staticmethod
    def _compute_confidence(bio_state: BiologicalState) -> float:  # type: ignore
        """基于多巴胺、能量、皮质醇计算规划信心"""
        # 高多巴胺 = 信心足
        # 高皮质醇 = 谨慎（信心下降）
        # 低能量 = 不确定（信心下降）
        confidence = (
            0.5
            + bio_state.hormones.dopamine * 0.3
            - bio_state.hormones.cortisol * 0.2
            - (1.0 - bio_state.energy) * 0.1
        )
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _classify_energy(energy: float) -> str:
        """将能量值分类为人类可理解的状态"""
        if energy > 0.7:
            return "充足"
        elif energy > 0.5:
            return "正常"
        elif energy > 0.3:
            return "疲倦"
        else:
            return "濒死"

    @staticmethod
    def _explain_current_state(bio_state: BiologicalState) -> str:  # type: ignore
        """根据激素和能量，解释为什么做当前这件事"""
        reasons = []

        # 多巴胺
        if bio_state.hormones.dopamine > 0.7:
            reasons.append("多巴胺充足，充满动力")
        elif bio_state.hormones.dopamine < 0.3:
            reasons.append("多巴胺低落，比较被动")

        # 能量
        if bio_state.energy > 0.7:
            reasons.append("精力充沛")
        elif bio_state.energy < 0.3:
            reasons.append("能量紧张")

        # 社交
        if bio_state.social_deficit > 0.6:
            reasons.append("有社交需求")

        # 无聊感
        if bio_state.novelty_craving > 0.6:
            reasons.append("想尝试新东西")

        # 皮质醇
        if bio_state.hormones.cortisol > 0.8:
            reasons.append("皮质醇高，保持谨慎")

        return "；".join(reasons) if reasons else "保持常态"

    @staticmethod
    def _identify_obstacles(bio_state: BiologicalState) -> list[str]:  # type: ignore
        """识别当前行动的障碍"""
        obstacles = []

        if bio_state.energy < 0.3:
            obstacles.append("能量濒死")
        elif bio_state.energy < 0.5:
            obstacles.append("能量不足")

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
    ) -> str | None:
        """基于当前状态预测下一步可能的行动"""
        # 如果能量马上要用尽
        if bio_state.energy < 0.15:
            return "立即休息或进入梦境"

        # 如果压力积累很高
        if bio_state.allostatic_load > 0.7:
            return "寻求恢复（休息、社交）"

        # 如果社交需求很高
        if bio_state.social_deficit > 0.7:
            return "尝试社交互动"

        # 如果无聊感很高且能量充足
        if bio_state.novelty_craving > 0.6 and bio_state.energy > 0.6:
            return "主动发起新话题或探索"

        # 正常情况
        if current_task == "待机":
            if bio_state.energy > 0.7:
                return "寻找新任务"
            else:
                return "继续待机或进入浅度梦境"

        return None

    @staticmethod
    def system_prompt_modifier(bio_state: BiologicalState) -> str:  # type: ignore
        """生成给 LLM 的系统提示词修饰语（基于当前认知状态）"""
        modifiers = []

        # 基于能量
        if bio_state.energy > 0.7:
            modifiers.append("你精力充沛、反应迅速")
        elif bio_state.energy < 0.3:
            modifiers.append("你感到疲惫、反应缓慢")

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

        # 基于社交需求
        if bio_state.social_deficit > 0.6:
            modifiers.append("你渴望交流、更显亲近")

        # 基于无聊感
        if bio_state.novelty_craving > 0.6:
            modifiers.append("你对新鲜事物有兴趣")

        if not modifiers:
            modifiers.append("你感到平静、状态正常")

        return "；".join(modifiers) + "。"
