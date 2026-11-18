"""认知透明层单元测试"""

from datetime import UTC, datetime

import pytest

from aureus.common.clock import ManualClock
from aureus.core.biomodel import BiologicalState, HormoneState
from aureus.core.cognitive_layer import CognitiveObserver, CognitiveState


class TestCognitiveState:
    """CognitiveState 数据类测试"""

    def test_initialization(self):
        """测试初始化"""
        cognitive = CognitiveState(current_task="回复用户", planning_depth=2)
        assert cognitive.current_task == "回复用户"
        assert cognitive.planning_depth == 2
        assert cognitive.energy_state == "normal"

    def test_self_awareness_statement_basic(self):
        """测试基础的自我意识陈述"""
        cognitive = CognitiveState(
            current_task="思考",
            planning_depth=3,
            why_this_decision="多巴胺充足",
            obstacles=[],
        )
        statement = cognitive.self_awareness_statement()
        assert "我现在在思考" in statement
        assert "多巴胺充足" in statement
        assert "没有明显障碍" in statement

    def test_self_awareness_statement_with_obstacles(self):
        """测试带障碍的自我意识陈述"""
        cognitive = CognitiveState(
            current_task="工作",
            why_this_decision="有承诺",
            obstacles=["能量不足", "皮质醇高"],
        )
        statement = cognitive.self_awareness_statement()
        assert "工作" in statement
        assert "能量不足" in statement
        assert "皮质醇高" in statement

    def test_self_awareness_statement_idle(self):
        """测试待机状态"""
        cognitive = CognitiveState(current_task="")
        statement = cognitive.self_awareness_statement()
        assert "待机" in statement

    def test_to_dict(self):
        """测试序列化为字典"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        cognitive = CognitiveState(
            current_task="测试",
            planning_depth=1,
            energy_state="充足",
            last_updated=clock.now(),
        )
        data = cognitive.to_dict()
        assert data["current_task"] == "测试"
        assert data["planning_depth"] == 1
        assert data["energy_state"] == "充足"
        assert data["last_updated"] is not None


class TestCognitiveObserver:
    """CognitiveObserver 观察器测试"""

    def test_initialization(self):
        """测试初始化"""
        observer = CognitiveObserver()
        assert observer.clock is not None

    def test_observe_basic(self):
        """测试基础观察"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)
        observer = CognitiveObserver(clock)

        cognitive = observer.observe(bio, {"task_name": "聊天", "planning_depth": 2})

        assert cognitive.current_task == "聊天"
        assert cognitive.planning_depth == 2
        assert cognitive.last_updated is not None

    def test_confidence_computation(self):
        """测试规划信心计算"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))

        # 高多巴胺 + 充足能量 = 高信心
        bio_confident = BiologicalState(
            hormones=HormoneState(dopamine=0.9, cortisol=0.2, serotonin=0.5, noradrenaline=0.4),
            energy=0.8,
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        cognitive1 = observer.observe(bio_confident, {})
        confidence1 = cognitive1.planning_confidence

        # 低多巴胺 + 低能量 + 高皮质醇 = 低信心
        bio_anxious = BiologicalState(
            hormones=HormoneState(dopamine=0.1, cortisol=0.9, serotonin=0.3, noradrenaline=0.2),
            energy=0.2,
            clock=clock,
        )
        cognitive2 = observer.observe(bio_anxious, {})
        confidence2 = cognitive2.planning_confidence

        assert confidence1 > confidence2

    def test_energy_classification(self):
        """测试能量分类"""
        observer = CognitiveObserver()

        assert observer._classify_energy(0.9) == "充足"
        assert observer._classify_energy(0.6) == "正常"
        assert observer._classify_energy(0.4) == "疲倦"
        assert observer._classify_energy(0.1) == "濒死"

    def test_explain_current_state_high_dopamine(self):
        """测试高多巴胺的解释"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            hormones=HormoneState(dopamine=0.8, cortisol=0.3, serotonin=0.5, noradrenaline=0.5),
            energy=0.7,
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        explanation = observer._explain_current_state(bio)
        assert "多巴胺" in explanation or "精力充沛" in explanation

    def test_explain_current_state_high_social_need(self):
        """测试高社交需求的解释"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            social_deficit=0.8,
            hormones=HormoneState(dopamine=0.5, cortisol=0.4, serotonin=0.5, noradrenaline=0.4),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        explanation = observer._explain_current_state(bio)
        assert "社交需求" in explanation

    def test_identify_obstacles_low_energy(self):
        """测试低能量障碍识别"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            energy=0.2,
            hormones=HormoneState(dopamine=0.5, cortisol=0.4, serotonin=0.5, noradrenaline=0.4),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        obstacles = observer._identify_obstacles(bio)
        assert "能量濒死" in obstacles

    def test_identify_obstacles_high_cortisol(self):
        """测试高皮质醇障碍识别"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            energy=0.5,
            hormones=HormoneState(dopamine=0.5, cortisol=0.9, serotonin=0.5, noradrenaline=0.4),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        obstacles = observer._identify_obstacles(bio)
        # 检查是否有皮质醇相关的障碍（可能是"皮质醇高"或"皮质醇过高，焦虑模式"）
        assert any("皮质醇" in obs for obs in obstacles)

    def test_identify_obstacles_multiple(self):
        """测试多障碍识别"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            energy=0.2,
            hormones=HormoneState(dopamine=0.5, cortisol=0.85, serotonin=0.2, noradrenaline=0.4),
            allostatic_load=0.75,
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        obstacles = observer._identify_obstacles(bio)
        assert len(obstacles) >= 3
        assert "能量濒死" in obstacles
        assert any("皮质醇" in obs for obs in obstacles)
        assert any("血清素" in obs for obs in obstacles)

    def test_predict_next_action_energy_critical(self):
        """测试濒死状态下的行动预测"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(energy=0.1, clock=clock)
        observer = CognitiveObserver(clock)
        action = observer._predict_next_action(bio, "工作")
        assert action is not None
        assert "休息" in action or "梦境" in action

    def test_predict_next_action_high_novelty(self):
        """测试高无聊感下的行动预测"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            novelty_craving=0.8,
            energy=0.8,
            hormones=HormoneState(dopamine=0.8, cortisol=0.3, serotonin=0.6, noradrenaline=0.6),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        action = observer._predict_next_action(bio, "待机")
        assert action is not None
        assert "新话题" in action or "探索" in action

    def test_system_prompt_modifier_high_energy(self):
        """测试高能量状态的系统提示词"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            energy=0.9,
            hormones=HormoneState(dopamine=0.8, cortisol=0.2, serotonin=0.7, noradrenaline=0.7),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        modifier = observer.system_prompt_modifier(bio)
        assert "精力充沛" in modifier
        assert "充满动力" in modifier

    def test_system_prompt_modifier_low_energy(self):
        """测试低能量状态的系统提示词"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            energy=0.1,
            hormones=HormoneState(dopamine=0.2, cortisol=0.8, serotonin=0.2, noradrenaline=0.2),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        modifier = observer.system_prompt_modifier(bio)
        assert "疲惫" in modifier

    def test_system_prompt_modifier_anxious(self):
        """测试焦虑状态的系统提示词"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            energy=0.5,
            hormones=HormoneState(dopamine=0.3, cortisol=0.9, serotonin=0.3, noradrenaline=0.5),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        modifier = observer.system_prompt_modifier(bio)
        assert "紧张" in modifier or "谨慎" in modifier


class TestBiologicalStateIntegration:
    """BiologicalState 与 CognitiveState 的集成测试"""

    def test_observe_cognitive_state(self):
        """测试 BiologicalState 的 observe_cognitive_state 方法"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)

        # 第一次观察
        cognitive1 = bio.observe_cognitive_state(
            {"task_name": "思考", "planning_depth": 3, "focus_target": "问题"}
        )

        assert cognitive1.current_task == "思考"
        assert cognitive1.planning_depth == 3
        assert cognitive1.attention_focus == "问题"
        assert cognitive1.last_updated is not None

        # 第二次观察（不同的激素状态）
        bio.hormones.dopamine = 0.8
        bio.energy = 0.2
        cognitive2 = bio.observe_cognitive_state({"task_name": "疲劳中"})

        assert cognitive2.current_task == "疲劳中"
        # energy_state 应该是"疲倦"或"濒死"（当 energy <= 0.3）
        assert cognitive2.energy_state in ("疲倦", "濒死")
        assert len(cognitive2.obstacles) > 0  # 能量低应该产生障碍

    def test_i_think_therefore_i_am(self):
        """测试"我思故我在"的完整实现"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            energy=0.7,
            hormones=HormoneState(dopamine=0.7, cortisol=0.3, serotonin=0.6, noradrenaline=0.6),
            clock=clock,
        )

        # 它观察自己
        cognitive = bio.observe_cognitive_state({"task_name": "回复用户", "planning_depth": 2})

        # 生成自我意识陈述
        statement = cognitive.self_awareness_statement()

        # 验证它能说出自己在干什么
        assert "回复用户" in statement
        assert statement != ""  # 不是空的
        assert "。" in statement  # 是完整的句子

        # 它能列举自己的理由
        assert cognitive.why_this_decision != ""
        assert cognitive.active_hormones is not None

        print(f"它说：{statement}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
