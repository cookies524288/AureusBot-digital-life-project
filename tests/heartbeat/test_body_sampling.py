"""心跳定时记身体信号样本的测试：D64 第 1、3、4 条。

每隔 [body] sample_interval_seconds 记一行；关掉的信号存 NULL；总开关关掉就不记；
写样本失败只记日志，心跳照常跳，也不会每一拍都重试。
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureus.body import Body, BodySample, BodySampleStore
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, ManualClock
from aureus.heartbeat import Heartbeat, Perception, TrustLevel
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase, VisibilityScope

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
CALENDAR = BudgetCalendar(UTC)
CHANNEL = "terminal"
INTERVAL = 60.0
WINDOW = timedelta(days=1)


class HoldingOrgans:
    """收到请求后一直等着，让「回复中」的状态和收件箱积压留在原地。"""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def call(self, request: OrganRequest) -> OrganResponse:
        await self.gate.wait()
        return OrganResponse(organ_id=request.organ_id, suggestion="嗯。")


class BrokenStore(BodySampleStore):
    """追加永远失败的存储，用来验证写失败不会拖垮心跳。"""

    def __init__(self, db_path: Path, clock: ManualClock) -> None:
        super().__init__(db_path, clock)
        self.attempts = 0

    def append(self, sample: BodySample) -> None:
        self.attempts += 1
        raise RuntimeError("磁盘满了")


@dataclass
class Rig:
    clock: ManualClock
    store: BodySampleStore
    organs: HoldingOrgans
    heartbeat: Heartbeat
    timeline: TimelineDatabase
    ledger: Ledger

    def samples(self) -> list[BodySample]:
        return self.store.since(START - WINDOW)

    def say(self, content: str) -> None:
        assert self.heartbeat.submit(
            Perception(
                channel=CHANNEL,
                sender_id="local",
                content=content,
                visibility=VisibilityScope.USER_ONLY,
                trust=TrustLevel.VERIFIED_USER,
            )
        )


@pytest.fixture
def made() -> Iterator[list[Rig]]:
    rigs: list[Rig] = []
    yield rigs
    for rig in rigs:
        rig.organs.gate.set()
        rig.store.close()
        rig.timeline.close()
        rig.ledger.close()


def make_rig(
    made: list[Rig],
    tmp_path: Path,
    *,
    with_store: bool = True,
    broken: bool = False,
    **body: Any,
) -> Rig:
    clock = ManualClock(START)
    db_path = tmp_path / "aureus.db"
    ledger = Ledger(db_path, clock, calendar=CALENDAR)
    timeline = TimelineDatabase(db_path, clock)
    ledger.open()
    timeline.open()
    store: BodySampleStore = (
        BrokenStore(db_path, clock) if broken else BodySampleStore(db_path, clock)
    )
    store.open()
    config = BodyConfig(**{"sample_interval_seconds": INTERVAL, **body})
    emergency_stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = HoldingOrgans()
    heartbeat = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(
            {
                "facts": {"name": "小绿", "birthday": "2008-01-01"},
                "user": {"name": "阿明"},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(tick_interval_seconds=INTERVAL, planning_enabled=False),
        permissions=PermissionChecker(emergency_stop),
        emergency_stop=emergency_stop,
        timezone=UTC,
        body=Body(config, None, ledger, clock, CALENDAR),
        samples=store if with_store else None,
    )
    heartbeat.attach_channel(CHANNEL, lambda message: None)
    heartbeat.wake_up()
    rig = Rig(clock, store, organs, heartbeat, timeline, ledger)
    made.append(rig)
    return rig


async def test_first_beat_records_a_sample(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    await rig.heartbeat.beat()
    [sample] = rig.samples()
    assert sample.timestamp == START
    assert sample.energy_tier == "不限"
    assert sample.energy_fraction is None
    assert sample.inbox_count == 0


async def test_samples_follow_the_interval(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    await rig.heartbeat.beat()
    rig.clock.advance(timedelta(seconds=INTERVAL - 1))
    await rig.heartbeat.beat()
    assert len(rig.samples()) == 1  # 还没到间隔
    rig.clock.advance(timedelta(seconds=1))
    await rig.heartbeat.beat()
    assert len(rig.samples()) == 2
    rig.clock.advance(timedelta(seconds=INTERVAL * 3))
    await rig.heartbeat.beat()
    assert len(rig.samples()) == 3  # 跳过的间隔不补记


async def test_sample_switch_off_records_nothing(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, sample_enabled=False)
    await rig.heartbeat.beat()
    rig.clock.advance(timedelta(seconds=INTERVAL * 2))
    await rig.heartbeat.beat()
    assert rig.samples() == []


async def test_heartbeat_without_a_store_still_beats(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, with_store=False)
    await rig.heartbeat.beat()
    assert rig.samples() == []


async def test_disabled_signals_are_stored_as_none(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, fatigue_enabled=False, stress_enabled=False, sleep_enabled=False)
    await rig.heartbeat.beat()
    [sample] = rig.samples()
    assert sample.fatigue_level is None
    assert sample.stress_level is None and sample.backlog is None
    assert sample.night is None


async def test_enabled_signals_are_stored_as_numbers(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    await rig.heartbeat.beat()
    [sample] = rig.samples()
    assert sample.fatigue_level == 0.0
    assert sample.stress_level == 0.0 and sample.backlog == 0
    assert sample.night is False  # 当地正午


async def test_sample_counts_waiting_messages_and_names_the_focus(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.say("在吗")
    await rig.heartbeat.beat()  # 开始回复，器官被拦着不返回
    await asyncio.sleep(0)
    rig.clock.advance(timedelta(seconds=INTERVAL))
    rig.say("还有一件事")
    await rig.heartbeat.beat()  # 回复中又来一条：在收件箱里排队
    latest = rig.store.latest()
    assert latest is not None
    assert latest.inbox_count >= 1
    assert latest.focus == "阿明"  # 回复中，焦点是正在对话的用户


async def test_failed_write_does_not_stop_the_heartbeat(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, broken=True)
    assert isinstance(rig.store, BrokenStore)
    await rig.heartbeat.beat()
    await rig.heartbeat.beat()
    await rig.heartbeat.beat()  # 都没有抛异常
    # 失败后也算记过一次：没到间隔不重试，免得库出问题时每拍刷日志。
    assert rig.store.attempts == 1
    rig.clock.advance(timedelta(seconds=INTERVAL))
    await rig.heartbeat.beat()
    assert rig.store.attempts == 2


async def test_clock_moving_backwards_records_again(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    await rig.heartbeat.beat()
    # ManualClock 不能往回拨，所以直接造出时钟回拨后的状态：上次记样本在「现在」之后。
    rig.heartbeat._last_sample_at = START + timedelta(hours=1)
    await rig.heartbeat.beat()
    assert len(rig.samples()) == 2
