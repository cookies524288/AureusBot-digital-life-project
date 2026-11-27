"""核心组装接上身体信号样本的测试（路线图 1.11 步骤 4，D64）。

启动时清掉超过保留期限的样本；组装好的心跳跳一拍就记一行；总开关关掉就不记。
"""

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.body import BodySample, BodySampleStore
from aureus.common import AureusPaths, ManualClock, shutdown_logging
from aureus.core.runtime import open_core

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
WINDOW = timedelta(days=365)

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


def make_sample(moment: datetime, inbox_count: int) -> BodySample:
    return BodySample(
        timestamp=moment,
        energy_tier="不限",
        energy_fraction=None,
        used_today=None,
        daily_quota=None,
        fatigue_level=0.0,
        stress_level=0.0,
        night=False,
        focus="",
        inbox_count=inbox_count,
    )


def saved_samples(paths: AureusPaths) -> list[BodySample]:
    store = BodySampleStore(paths.database_file, ManualClock(START))
    store.open()
    try:
        return store.since(START - WINDOW)
    finally:
        store.close()


async def test_a_beat_on_the_assembled_core_records_a_sample(paths: AureusPaths) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        core.heartbeat.wake_up()
        await core.heartbeat.beat()
    [sample] = saved_samples(paths)
    assert sample.timestamp == START
    assert sample.energy_tier == "不限"


async def test_startup_prunes_samples_older_than_the_retention(paths: AureusPaths) -> None:
    store = BodySampleStore(paths.database_file, ManualClock(START))
    store.open()
    store.append(make_sample(START - timedelta(days=31), inbox_count=1))
    store.append(make_sample(START - timedelta(days=29), inbox_count=2))
    store.close()
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False):
        pass
    assert [sample.inbox_count for sample in saved_samples(paths)] == [2]


async def test_retention_comes_from_the_config(paths: AureusPaths) -> None:
    paths.runtime_config_file.write_text("[body]\nsample_retention_days = 3\n", encoding="utf-8")
    store = BodySampleStore(paths.database_file, ManualClock(START))
    store.open()
    store.append(make_sample(START - timedelta(days=4), inbox_count=1))
    store.append(make_sample(START - timedelta(days=2), inbox_count=2))
    store.close()
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False):
        pass
    assert [sample.inbox_count for sample in saved_samples(paths)] == [2]


async def test_sample_switch_off_records_nothing_but_still_assembles(paths: AureusPaths) -> None:
    paths.runtime_config_file.write_text("[body]\nsample_enabled = false\n", encoding="utf-8")
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        core.heartbeat.wake_up()
        await core.heartbeat.beat()
    assert saved_samples(paths) == []
