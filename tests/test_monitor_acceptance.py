"""1.11 验收（docs/08-roadmap.md，D64）：观察台开着时调低预算，曲线随之变化；观察台对数据库只读。

走 aureus chat 实际用的 open_core 组装路径，器官用假的，不花钱。心跳按间隔把身体读数记成样本，
观察台的只读读取器把样本读回来：预算调低后，同样聊一句，精力比例和档位都不一样。
"""

import sqlite3
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.body import BodySample
from aureus.channels import CHANNEL_NAME, TERMINAL_USER_ID
from aureus.common import AureusPaths, ManualClock, shutdown_logging
from aureus.core.runtime import Core, open_core
from aureus.heartbeat import Perception, TrustLevel, VisibilityScope
from aureus.monitor import MonitorReader
from aureus.organs import CallAttempt, FakeReplyer, OrganRequest, OrganResponse

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
SAMPLE_INTERVAL = timedelta(seconds=61)  # 比默认的 60 秒间隔多一点
HIGH_LIMIT = 100_000
LOW_LIMIT = 100

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


@pytest.fixture(autouse=True)
def _fake_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """让假回复器每次报 6 个 token，账本里才有用量，精力才会变。"""

    async def process(self: FakeReplyer, request: OrganRequest) -> OrganResponse:
        attempt = CallAttempt("fake", "fake", prompt_tokens=4, completion_tokens=2)
        return OrganResponse(self.organ_id, suggestion="嗯。", attempts=[attempt])

    monkeypatch.setattr(FakeReplyer, "process", process)


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    return paths


def set_limit(paths: AureusPaths, limit: int) -> None:
    text = f"[safety]\nmonthly_token_limit = {limit}\n"
    paths.runtime_config_file.write_text(text, encoding="utf-8")


async def chat_once(core: Core, clock: ManualClock, text: str) -> None:
    """聊一句，等回复完成，再越过采样间隔跳一拍：这一拍记下的样本里已经有这次的用量。"""
    core.heartbeat.attach_channel(CHANNEL_NAME, lambda message: None)
    core.heartbeat.wake_up()
    assert core.heartbeat.submit(
        Perception(
            channel=CHANNEL_NAME,
            sender_id=TERMINAL_USER_ID,
            content=text,
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    core.heartbeat.beat()
    await core.heartbeat.wait_for_thought()
    core.heartbeat.beat()
    clock.advance(SAMPLE_INTERVAL)
    core.heartbeat.beat()


def latest_sample(paths: AureusPaths) -> BodySample:
    reader = MonitorReader(paths.database_file)
    reader.open()
    try:
        sample = reader.latest_sample()
    finally:
        reader.close()
    assert sample is not None
    return sample


async def test_lowering_the_budget_changes_the_energy_curve(paths: AureusPaths) -> None:
    clock = ManualClock(START)
    set_limit(paths, HIGH_LIMIT)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        await chat_once(core, clock, "你好")
        await core.heartbeat.shut_down()
    before = latest_sample(paths)

    # 观察台开着的时候用户把预算调低，重启后同样聊一句。
    set_limit(paths, LOW_LIMIT)
    clock.advance(timedelta(minutes=5))
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        await chat_once(core, clock, "在吗")
        await core.heartbeat.shut_down()
    after = latest_sample(paths)

    assert before.energy_tier == "正常"
    assert before.energy_fraction is not None and before.energy_fraction > 0.9
    assert after.energy_fraction is not None
    assert after.energy_fraction < before.energy_fraction
    # 第二次启动时账本里已有第一次的用量，所以可能是低电量，也可能掉到透支保护：
    # 验收只要求预算调低后档位不再是正常档，精力明显更低。
    assert after.energy_tier != "正常"
    assert after.energy_fraction < 0.2
    assert after.daily_quota is not None and before.daily_quota is not None
    assert after.daily_quota < before.daily_quota


async def test_the_curve_keeps_every_sample_in_order(paths: AureusPaths) -> None:
    clock = ManualClock(START)
    set_limit(paths, HIGH_LIMIT)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        await chat_once(core, clock, "你好")
        await core.heartbeat.shut_down()
    reader = MonitorReader(paths.database_file)
    reader.open()
    try:
        samples = reader.samples_since(START - timedelta(days=1))
    finally:
        reader.close()
    assert len(samples) >= 2
    assert [s.timestamp for s in samples] == sorted(s.timestamp for s in samples)


async def test_the_monitor_connection_cannot_write(paths: AureusPaths) -> None:
    clock = ManualClock(START)
    set_limit(paths, HIGH_LIMIT)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        await chat_once(core, clock, "你好")
        await core.heartbeat.shut_down()
    reader = MonitorReader(paths.database_file)
    reader.open()
    try:
        conn = reader._conn
        assert conn is not None
        for sql in (
            "DELETE FROM body_samples",
            "UPDATE ledger SET cost = 0",
            "DROP TABLE timeline",
        ):
            with pytest.raises(sqlite3.Error):
                conn.execute(sql)
    finally:
        reader.close()
    assert latest_sample(paths) is not None  # 样本还在
