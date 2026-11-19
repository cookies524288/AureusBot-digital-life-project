"""认知透明层 - 让它能观察自己的思考过程

实现"我思故我在"：通过自我观察元认知，它能知道自己在思考什么、为什么这样想。

参考：D34, docs/18-neuroscience-completeness.md
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aureus.common.clock import Clock, SystemClock
from aureus.feedback import FeedbackError

if TYPE_CHECKING:
    from aureus.body.energy import EnergyReading
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

    # 此刻各种激素的浓度 [0, 1]（D69：怒气、愉悦、低落、惊讶；基线都是 0）。
    # 激素关着（或扮演模式）时是空字典。
    active_hormones: dict[str, float] = field(default_factory=dict)

    # 精力来自身体信号（预算），不是模拟的量（D53、D56）：档位名，和精力比例 [0, 1]；
    # 没设月度上限（不限）时比例是 None。
    energy_state: str = _TIER_UNLIMITED
    energy_level: float | None = None

    attention_focus: str = ""  # 注意力聚焦在哪：用户名、任务、想法等

    # 内省：为什么做这个决定？
    why_this_decision: str = ""  # 决策理由（自然语言）
    obstacles: list[str] = field(default_factory=list)  # ["精力低电量", ...]
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
    """观察器：让它能观察自己的思考过程；精力读身体信号，激素读激素浓度，信心读反馈。"""

    def __init__(self, clock: Clock | None = None, feedback: FeedbackStore | None = None):
        self.clock = clock or SystemClock()
        # 不给反馈存储（或 [feedback] 关闭）时，信心一律显示没有数据。
        self._feedback = feedback

    def observe(
        self,
        context: dict[str, Any] | None = None,
        energy: EnergyReading | None = None,
        hormone_levels: Mapping[str, float] | None = None,
    ) -> CognitiveState:
        """根据身体信号、激素浓度和当前上下文，生成它对自己的观察

        Args:
            context: 当前执行上下文（任务名、规划深度、反馈类别 feedback_category 等）
            energy: 身体的精力读数；不给时当作「不限」。
            hormone_levels: 此刻各激素的浓度（键是激素名）；不给表示激素没开。

        Returns:
            CognitiveState：对当前认知状态的描述
        """
        context = context or {}
        current_task = context.get("task_name", "待机")
        planning_depth = context.get("planning_depth", 0)
        # 规划信心：读反馈里这一类事情的真实成功率（D67）
        planning_confidence, confidence_note = self._read_confidence(
            context.get("feedback_category")
        )
        # 精力：直接用身体给的档位，不在这里另算
        energy_state = energy.tier.value if energy is not None else _TIER_UNLIMITED
        energy_level = energy.fraction if energy is not None else None
        return CognitiveState(
            current_task=current_task,
            planning_depth=planning_depth,
            planning_confidence=planning_confidence,
            confidence_note=confidence_note,
            active_hormones=dict(hormone_levels or {}),
            energy_state=energy_state,
            energy_level=energy_level,
            attention_focus=context.get("focus_target", ""),
            why_this_decision=self._explain_current_state(energy_state),
            obstacles=self._identify_obstacles(energy_state),
            next_likely_action=self._predict_next_action(current_task, energy_state),
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
    def _explain_current_state(energy_state: str) -> str:
        """根据精力档位，解释为什么做当前这件事"""
        if energy_state in (_TIER_SAVING, _TIER_LOW, _TIER_OVERDRAFT):
            return f"今天的精力额度紧张（{energy_state}档）"
        return "保持常态"

    @staticmethod
    def _identify_obstacles(energy_state: str) -> list[str]:
        """识别当前行动的障碍"""
        if energy_state == _TIER_OVERDRAFT:
            return ["今天的额度几乎用完（透支保护）"]
        if energy_state == _TIER_LOW:
            return ["精力低电量"]
        if energy_state == _TIER_SAVING:
            return ["精力进入节省档"]
        return []

    @staticmethod
    def _predict_next_action(current_task: str, energy_state: str) -> str | None:
        """基于当前状态预测下一步可能的行动"""
        # 今天的额度快用完：只回应用户，其余留到明天
        if energy_state in (_TIER_LOW, _TIER_OVERDRAFT):
            return "只回应用户，其余的事留到明天"
        # 待机时：额度紧张就继续待着，否则找事做
        if current_task == "待机":
            if energy_state == _TIER_SAVING:
                return "继续待机"
            return "寻找新任务"
        return None
