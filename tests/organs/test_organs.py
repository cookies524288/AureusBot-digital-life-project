"""测试器官接口和总线"""

from datetime import UTC, datetime

import pytest

from aureus.common.clock import ManualClock
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
    async def test_send_to_registered_organ(self, organ_bus):
        """测试发送请求到已注册器官"""
        replyer = FakeReplyer()
        organ_bus.register(replyer)

        request = OrganRequest(
            organ_id="fake_replyer",
            prompt="测试提示词",
        )
        response = await organ_bus.send(request)

        assert isinstance(response, OrganResponse)
        assert response.organ_id == "fake_replyer"
        assert "测试提示词" in response.suggestion

    @pytest.mark.asyncio
    async def test_send_to_unregistered_raises(self, organ_bus):
        """测试发送到未注册器官应抛出异常"""
        request = OrganRequest(
            organ_id="nonexistent",
            prompt="测试",
        )

        with pytest.raises(ValueError, match="未注册"):
            await organ_bus.send(request)


class TestFakeReplyer:
    """测试假回复器官"""

    @pytest.mark.asyncio
    async def test_returns_template_reply(self):
        """返回带提示词开头的固定模板，置信度大于 0"""
        replyer = FakeReplyer()
        request = OrganRequest(organ_id="fake_replyer", prompt="你好")
        response = await replyer.process(request)

        assert "你好" in response.suggestion
        assert response.confidence > 0
        assert response.metadata == {"type": "fake"}


class TestFakePlanner:
    """测试假规划器官"""

    @pytest.mark.asyncio
    async def test_returns_fixed_plan(self):
        """返回固定的规划建议"""
        planner = FakePlanner()
        request = OrganRequest(organ_id="fake_planner", prompt="规划任务")
        response = await planner.process(request)

        assert len(response.suggestion) > 0
        assert response.confidence > 0
        assert response.metadata == {"type": "fake"}
