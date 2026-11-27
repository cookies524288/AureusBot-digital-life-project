"""核心组装接上反馈的测试（路线图 2.1 步骤 4，D67）。

开关打开时创建反馈存储并交给心跳；关掉就不创建，认知状态的信心显示没有数据。
"""

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import AureusPaths, ManualClock, shutdown_logging
from aureus.core.runtime import open_core
from aureus.feedback import FeedbackStore

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)

IDENTITY = textwrap.dedent(
    """\
    [facts]
    name = "测试助手"
    birthday = "2008-01-01"

    [user]
    name = "测试用户"

    [persona]
    immutable = "你是一个测试用的数字生命。"
    """
)


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    return paths


async def test_feedback_store_is_assembled_by_default(paths: AureusPaths) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        assert isinstance(core.feedback, FeedbackStore)
        # 打开过：能正常查询，没有历史时如实说没有数据。
        assert core.feedback.confidence("sim_task:easy").value is None


async def test_confidence_shows_no_data_on_the_assembled_heartbeat(paths: AureusPaths) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        core.heartbeat.wake_up()
        await core.heartbeat.beat()
        state = core.heartbeat.cognitive_state
    assert state is not None
    assert state.planning_confidence is None
    assert state.confidence_note == "没有数据"


async def test_feedback_switch_off_creates_no_store(paths: AureusPaths) -> None:
    paths.runtime_config_file.write_text("[feedback]\nenabled = false\n", encoding="utf-8")
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        assert core.feedback is None
        core.heartbeat.wake_up()
        await core.heartbeat.beat()
        assert core.heartbeat.cognitive_state is not None
