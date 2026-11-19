"""认知透明层单元测试"""

from datetime import UTC, datetime

import pytest

from aureus.body.energy import UNLIMITED_READING, EnergyReading, EnergyTier
from aureus.common import FeedbackConfig
from aureus.common.clock import ManualClock
from aureus.core.biomodel import BiologicalState, HormoneState
from aureus.core.cognitive_layer import CognitiveObserver, CognitiveState
from aureus.feedback import FeedbackStore, Outcome


def reading(tier: EnergyTier, fraction: float) -> EnergyReading:
    return EnergyReading(tier=tier, fraction=fraction)


class TestCognitiveState:
    """CognitiveState 数据类测试"""

    def test_initialization(self):
        """测试初始化"""
        cognitive = CognitiveState(current_task="回复用户", planning_depth=2)
        assert cognitive.current_task == "回复用户"
        assert cognitive.planning_depth == 2
        assert cognitive.energy_state == "不限"  # 没有身体读数时按不限算
        assert cognitive.energy_level is None

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
            obstacles=["精力低电量", "皮质醇高"],
        )
        statement = cognitive.self_awareness_statement()
        assert "工作" in statement
        assert "精力低电量" in statement
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
            energy_state="节省",
            last_updated=clock.now(),
        )
        data = cognitive.to_dict()
        assert data["current_task"] == "测试"
        assert data["planning_depth"] == 1
        assert data["energy_state"] == "节省"
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

    def test_confidence_has_no_data_without_feedback(self):
        """没有反馈存储时信心显示没有数据，激素再高也不拼一个出来（D67）"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio_confident = BiologicalState(
            hormones=HormoneState(dopamine=0.9, cortisol=0.2, serotonin=0.5, noradrenaline=0.4),
            clock=clock,
        )
        cognitive = CognitiveObserver(clock).observe(
            bio_confident, {"feedback_category": "sim_task:easy"}
        )
        assert cognitive.planning_confidence is None
        assert cognitive.confidence_note == "没有数据"

    def test_confidence_comes_from_feedback_history(self, tmp_path):
        """信心取反馈里该类事情最近的真实成功率，和激素无关"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        store = FeedbackStore(tmp_path / "aureus.db", FeedbackConfig(min_samples=4), clock)
        store.open()
        try:
            for outcome in (Outcome.SUCCESS, Outcome.SUCCESS, Outcome.SUCCESS, Outcome.FAILURE):
                store.record_outcome(store.record_expectation("sim_task:easy", 0.5), outcome)
            observer = CognitiveObserver(clock, store)
            calm = BiologicalState(clock=clock)
            tense = BiologicalState(
                hormones=HormoneState(dopamine=0.1, cortisol=0.9, serotonin=0.3, noradrenaline=0.2),
                clock=clock,
            )
            context = {"feedback_category": "sim_task:easy"}
            assert observer.observe(calm, context).planning_confidence == 0.75
            assert observer.observe(tense, context).planning_confidence == 0.75
            # 没指定类别，或这一类没有历史：没有数据
            assert observer.observe(calm, {}).planning_confidence is None
            other = observer.observe(calm, {"feedback_category": "sim_task:other"})
            assert other.planning_confidence is None
            # 类别不认识（反馈报错）时朝安全方向倒，不抛出
            bad = observer.observe(calm, {"feedback_category": "乱起的名字"})
            assert bad.planning_confidence is None
        finally:
            store.close()

    def test_energy_comes_from_the_body_reading(self):
        """精力档位和比例直接取身体的读数，不另算"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)
        observer = CognitiveObserver(clock)

        cognitive = observer.observe(bio, {}, reading(EnergyTier.LOW, 0.12))
        assert cognitive.energy_state == "低电量"
        assert cognitive.energy_level == 0.12

        unlimited = observer.observe(bio, {}, UNLIMITED_READING)
        assert unlimited.energy_state == "不限"
        assert unlimited.energy_level is None  # 不限时没有比例，不假装有

    def test_energy_does_not_change_planning_confidence(self):
        """精力不参与信心：信心只读反馈（D56 去掉了模拟精力，D67 去掉了激素拼信心）"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)
        observer = CognitiveObserver(clock)
        rich = observer.observe(bio, {}, reading(EnergyTier.NORMAL, 0.9))
        poor = observer.observe(bio, {}, reading(EnergyTier.OVERDRAFT, 0.02))
        assert rich.planning_confidence == poor.planning_confidence

    def test_explain_current_state_high_dopamine(self):
        """测试高多巴胺的解释"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            hormones=HormoneState(dopamine=0.8, cortisol=0.3, serotonin=0.5, noradrenaline=0.5),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        explanation = observer._explain_current_state(bio, "正常")
        assert "多巴胺" in explanation

    def test_explain_current_state_tight_energy(self):
        """额度紧张的档位要出现在解释里；正常档和不限不出现"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            hormones=HormoneState(dopamine=0.5, cortisol=0.4, serotonin=0.5, noradrenaline=0.4),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        assert "额度紧张" in observer._explain_current_state(bio, "节省")
        assert "额度紧张" not in observer._explain_current_state(bio, "正常")
        assert "额度紧张" not in observer._explain_current_state(bio, "不限")

    def test_identify_obstacles_low_energy(self):
        """测试低精力障碍识别：三个紧张档各有各的说法，正常和不限没有"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            hormones=HormoneState(dopamine=0.5, cortisol=0.4, serotonin=0.5, noradrenaline=0.4),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        assert "透支保护" in observer._identify_obstacles(bio, "透支保护")[0]
        assert "低电量" in observer._identify_obstacles(bio, "低电量")[0]
        assert "节省" in observer._identify_obstacles(bio, "节省")[0]
        assert observer._identify_obstacles(bio, "正常") == []
        assert observer._identify_obstacles(bio, "不限") == []

    def test_identify_obstacles_high_cortisol(self):
        """测试高皮质醇障碍识别"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            hormones=HormoneState(dopamine=0.5, cortisol=0.9, serotonin=0.5, noradrenaline=0.4),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        obstacles = observer._identify_obstacles(bio, "正常")
        # 检查是否有皮质醇相关的障碍（可能是"皮质醇高"或"皮质醇过高，焦虑模式"）
        assert any("皮质醇" in obs for obs in obstacles)

    def test_identify_obstacles_multiple(self):
        """测试多障碍识别"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            hormones=HormoneState(dopamine=0.5, cortisol=0.85, serotonin=0.2, noradrenaline=0.4),
            allostatic_load=0.75,
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        obstacles = observer._identify_obstacles(bio, "低电量")
        assert len(obstacles) >= 3
        assert "精力低电量" in obstacles
        assert any("皮质醇" in obs for obs in obstacles)
        assert any("血清素" in obs for obs in obstacles)

    def test_predict_next_action_tight_energy(self):
        """额度快用完：只回应用户，其余留到明天"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)
        observer = CognitiveObserver(clock)
        for tier in ("低电量", "透支保护"):
            action = observer._predict_next_action(bio, "工作", tier)
            assert action is not None
            assert "只回应用户" in action

    def test_predict_next_action_idle(self):
        """待机时：节省档继续待着，正常档和不限找事做；忙着时不预测"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)
        observer = CognitiveObserver(clock)
        assert observer._predict_next_action(bio, "待机", "节省") == "继续待机"
        assert observer._predict_next_action(bio, "待机", "正常") == "寻找新任务"
        assert observer._predict_next_action(bio, "待机", "不限") == "寻找新任务"
        assert observer._predict_next_action(bio, "工作", "正常") is None

    def test_system_prompt_modifier_high_dopamine(self):
        """测试高多巴胺状态的系统提示词"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
            hormones=HormoneState(dopamine=0.8, cortisol=0.2, serotonin=0.7, noradrenaline=0.7),
            clock=clock,
        )
        observer = CognitiveObserver(clock)
        assert "充满动力" in observer.system_prompt_modifier(bio)

    def test_system_prompt_modifier_never_mentions_energy(self):
        """精力不再写进人设修饰语（它在提示词里单独、如实地说，见 organs）"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(clock=clock)
        modifier = CognitiveObserver(clock).system_prompt_modifier(bio)
        assert "精力" not in modifier
        assert "疲惫" not in modifier

    def test_system_prompt_modifier_anxious(self):
        """测试焦虑状态的系统提示词"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
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

        # 第二次观察（身体读数变成低电量）
        bio.hormones.dopamine = 0.8
        low = reading(EnergyTier.LOW, 0.1)
        cognitive2 = bio.observe_cognitive_state({"task_name": "疲劳中"}, low)

        assert cognitive2.current_task == "疲劳中"
        assert cognitive2.energy_state == "低电量"
        assert cognitive2.energy_level == 0.1
        assert len(cognitive2.obstacles) > 0  # 额度紧张应该产生障碍

    def test_i_think_therefore_i_am(self):
        """测试"我思故我在"的完整实现"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        bio = BiologicalState(
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
