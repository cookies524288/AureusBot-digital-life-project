"""认知透明层单元测试"""

from datetime import UTC, datetime

import pytest

from aureus.body.energy import UNLIMITED_READING, EnergyReading, EnergyTier
from aureus.common import FeedbackConfig
from aureus.common.clock import ManualClock
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
            why_this_decision="保持常态",
            obstacles=[],
        )
        statement = cognitive.self_awareness_statement()
        assert "我现在在思考" in statement
        assert "保持常态" in statement
        assert "没有明显障碍" in statement

    def test_self_awareness_statement_with_obstacles(self):
        """测试带障碍的自我意识陈述"""
        cognitive = CognitiveState(
            current_task="工作",
            why_this_decision="有承诺",
            obstacles=["精力低电量", "怒气高"],
        )
        statement = cognitive.self_awareness_statement()
        assert "工作" in statement
        assert "精力低电量" in statement
        assert "怒气高" in statement

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
        observer = CognitiveObserver()
        assert observer.clock is not None

    def test_observe_basic(self):
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        cognitive = CognitiveObserver(clock).observe(
            {"task_name": "聊天", "planning_depth": 2, "focus_target": "小明"}
        )
        assert cognitive.current_task == "聊天"
        assert cognitive.planning_depth == 2
        assert cognitive.attention_focus == "小明"
        assert cognitive.last_updated == clock.now()

    def test_confidence_has_no_data_without_feedback(self):
        """没有反馈存储时信心显示没有数据，不拼一个出来（D67）"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        cognitive = CognitiveObserver(clock).observe({"feedback_category": "sim_task:easy"})
        assert cognitive.planning_confidence is None
        assert cognitive.confidence_note == "没有数据"

    def test_confidence_comes_from_feedback_history(self, tmp_path):
        """信心取反馈里该类事情最近的真实成功率"""
        clock = ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))
        store = FeedbackStore(tmp_path / "aureus.db", FeedbackConfig(min_samples=4), clock)
        store.open()
        try:
            for outcome in (Outcome.SUCCESS, Outcome.SUCCESS, Outcome.SUCCESS, Outcome.FAILURE):
                store.record_outcome(store.record_expectation("sim_task:easy", 0.5), outcome)
            observer = CognitiveObserver(clock, store)
            assert (
                observer.observe({"feedback_category": "sim_task:easy"}).planning_confidence == 0.75
            )
            # 没指定类别，或这一类没有历史：没有数据
            assert observer.observe({}).planning_confidence is None
            other = observer.observe({"feedback_category": "sim_task:other"})
            assert other.planning_confidence is None
            # 类别不认识（反馈报错）时朝安全方向倒，不抛出
            assert observer.observe({"feedback_category": "乱起的名字"}).planning_confidence is None
        finally:
            store.close()

    def test_energy_comes_from_the_body_reading(self):
        """精力档位和比例直接取身体的读数，不另算"""
        observer = CognitiveObserver(ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC)))

        low = observer.observe({}, reading(EnergyTier.LOW, 0.12))
        assert low.energy_state == "低电量"
        assert low.energy_level == 0.12

        unlimited = observer.observe({}, UNLIMITED_READING)
        assert unlimited.energy_state == "不限"
        assert unlimited.energy_level is None  # 不限时没有比例，不假装有

    def test_energy_does_not_change_planning_confidence(self):
        """精力不参与信心：信心只读反馈"""
        observer = CognitiveObserver(ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC)))
        rich = observer.observe({}, reading(EnergyTier.NORMAL, 0.9))
        poor = observer.observe({}, reading(EnergyTier.OVERDRAFT, 0.02))
        assert rich.planning_confidence == poor.planning_confidence

    def test_hormone_levels_are_copied_as_given(self):
        """激素浓度原样记下；不给表示激素没开，是空字典"""
        observer = CognitiveObserver(ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC)))
        levels = {"anger": 0.3, "joy": 0.0}
        cognitive = observer.observe({}, hormone_levels=levels)
        assert cognitive.active_hormones == levels
        assert cognitive.active_hormones is not levels  # 是副本，不会被外面改动牵连
        assert observer.observe({}).active_hormones == {}

    @pytest.mark.parametrize(
        ("tier", "fragment"),
        [
            (EnergyTier.SAVING, "节省"),
            (EnergyTier.LOW, "低电量"),
            (EnergyTier.OVERDRAFT, "透支保护"),
        ],
    )
    def test_tight_budget_shows_up_as_reason_and_obstacle(self, tier, fragment):
        observer = CognitiveObserver(ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC)))
        cognitive = observer.observe({}, reading(tier, 0.1))
        assert fragment in cognitive.why_this_decision
        assert any(fragment in item for item in cognitive.obstacles)

    def test_normal_budget_has_no_obstacle(self):
        observer = CognitiveObserver(ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC)))
        cognitive = observer.observe({}, reading(EnergyTier.NORMAL, 0.9))
        assert cognitive.obstacles == []
        assert cognitive.why_this_decision == "保持常态"

    def test_next_action_prediction(self):
        observer = CognitiveObserver(ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC)))
        low = observer.observe({"task_name": "待机"}, reading(EnergyTier.LOW, 0.1))
        assert low.next_likely_action == "只回应用户，其余的事留到明天"
        saving = observer.observe({"task_name": "待机"}, reading(EnergyTier.SAVING, 0.3))
        assert saving.next_likely_action == "继续待机"
        normal = observer.observe({"task_name": "待机"}, reading(EnergyTier.NORMAL, 0.9))
        assert normal.next_likely_action == "寻找新任务"
        busy = observer.observe({"task_name": "回复用户"}, reading(EnergyTier.NORMAL, 0.9))
        assert busy.next_likely_action is None

    def test_self_awareness_statement_is_a_full_sentence(self):
        observer = CognitiveObserver(ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC)))
        cognitive = observer.observe({"task_name": "回复用户", "planning_depth": 2})
        statement = cognitive.self_awareness_statement()
        assert "回复用户" in statement
        assert statement.endswith("。")
