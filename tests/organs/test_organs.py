"""测试器官接口和总线"""

from datetime import UTC, datetime

import pytest

from aureus.common.clock import ManualClock
from aureus.core import BiologicalState
from aureus.organs import (
    FakePlanner,
    FakeReplyer,
    OrganBus,
    OrganRequest,
    OrganResponse,
)


@pytest.fixture
def clock():
    """提供时钟对象"""
    return ManualClock(datetime(2024, 1, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def bio_state(clock):
    """提供生物状态对象"""
    return BiologicalState(clock=clock)


@pytest.fixture
def organ_bus():
    """提供器官总线"""
    return OrganBus()


class TestOrganBus:
    """测试器官总线"""

    def test_register_organ(self, organ_bus):
        """测试注册器官"""
        replyer = FakeReplyer()
        organ_bus.register(replyer)

        assert organ_bus.is_registered("fake_replyer")
        assert "fake_replyer" in organ_bus.list_organs()

    def test_register_duplicate_raises(self, organ_bus):
        """测试重复注册应抛出异常"""
        replyer = FakeReplyer()
        organ_bus.register(replyer)

        with pytest.raises(ValueError, match="已注册"):
            organ_bus.register(replyer)

    def test_unregister_organ(self, organ_bus):
        """测试注销器官"""
        replyer = FakeReplyer()
        organ_bus.register(replyer)
        organ_bus.unregister("fake_replyer")

        assert not organ_bus.is_registered("fake_replyer")

    def test_unregister_nonexistent_raises(self, organ_bus):
        """测试注销不存在的器官应抛出异常"""
        with pytest.raises(ValueError, match="未注册"):
            organ_bus.unregister("nonexistent")

    @pytest.mark.asyncio
    async def test_send_to_registered_organ(self, organ_bus, bio_state):
        """测试发送请求到已注册器官"""
        replyer = FakeReplyer()
        organ_bus.register(replyer)

        request = OrganRequest(
            organ_id="fake_replyer",
            prompt="测试提示词",
            bio_state=bio_state,
        )
        response = await organ_bus.send(request)

        assert isinstance(response, OrganResponse)
        assert response.organ_id == "fake_replyer"
        assert "测试提示词" in response.suggestion

    @pytest.mark.asyncio
    async def test_send_to_unregistered_raises(self, organ_bus, bio_state):
        """测试发送到未注册器官应抛出异常"""
        request = OrganRequest(
            organ_id="nonexistent",
            prompt="测试",
            bio_state=bio_state,
        )

        with pytest.raises(ValueError, match="未注册"):
            await organ_bus.send(request)


class TestFakeReplyer:
    """测试假回复器官"""

    @pytest.mark.asyncio
    async def test_happy_emotion_reply(self, bio_state):
        """测试高效价情绪的回复"""
        # 设置高多巴胺，产生积极情绪
        bio_state.hormones.dopamine = 0.8
        replyer = FakeReplyer()

        request = OrganRequest(
            organ_id="fake_replyer",
            prompt="你好",
            bio_state=bio_state,
        )
        response = await replyer.process(request)

        # 验证确实返回了建议，而不是崩溃
        assert len(response.suggestion) > 0
        assert response.confidence > 0

    @pytest.mark.asyncio
    async def test_tired_emotion_reply(self, bio_state):
        """测试低效价情绪的回复"""
        # 设置高皮质醇，产生消极情绪
        bio_state.hormones.cortisol = 0.8
        replyer = FakeReplyer()

        request = OrganRequest(
            organ_id="fake_replyer",
            prompt="工作",
            bio_state=bio_state,
        )
        response = await replyer.process(request)

        # 验证确实返回了建议
        assert len(response.suggestion) > 0

    @pytest.mark.asyncio
    async def test_metadata_includes_emotion(self, bio_state):
        """测试元数据包含情绪信息"""
        replyer = FakeReplyer()
        request = OrganRequest(
            organ_id="fake_replyer",
            prompt="测试",
            bio_state=bio_state,
        )
        response = await replyer.process(request)

        assert "emotion_used" in response.metadata
        assert isinstance(response.metadata["emotion_used"], str)


class TestFakePlanner:
    """测试假规划器官"""

    @pytest.mark.asyncio
    async def test_high_energy_planning(self, bio_state):
        """测试高能量下的规划"""
        # 设置高能量
        bio_state.energy = 0.9
        planner = FakePlanner()

        request = OrganRequest(
            organ_id="fake_planner",
            prompt="规划任务",
            bio_state=bio_state,
        )
        response = await planner.process(request)

        # 验证返回了建议
        assert len(response.suggestion) > 0
        assert response.confidence > 0

    @pytest.mark.asyncio
    async def test_low_energy_planning(self, bio_state):
        """测试低能量下的规划"""
        # 设置低能量
        bio_state.energy = 0.2
        planner = FakePlanner()

        request = OrganRequest(
            organ_id="fake_planner",
            prompt="规划任务",
            bio_state=bio_state,
        )
        response = await planner.process(request)

        # 验证返回了建议
        assert len(response.suggestion) > 0

    @pytest.mark.asyncio
    async def test_metadata_includes_cognitive_state(self, bio_state):
        """测试元数据包含认知状态"""
        planner = FakePlanner()
        request = OrganRequest(
            organ_id="fake_planner",
            prompt="规划",
            bio_state=bio_state,
        )
        response = await planner.process(request)

        assert "energy_used" in response.metadata
        assert "planning_depth" in response.metadata
