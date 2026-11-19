"""激素接进心跳的测试（路线图 2.3 步骤 4a，D69）：判断结果变成触发，记进时间线，重启后还原。"""

from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, HeartbeatConfig, HormonesConfig, JudgeConfig, ManualClock
from aureus.heartbeat import Heartbeat
from aureus.hormones import HormoneKind, Hormones
from aureus.identity import Identity
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase, TimelineQuery, VisibilityScope
from tests.heartbeat.test_judging_flow import (
    CALENDAR,
    CHANNEL,
    GOOD_JUDGMENT,
    MONTHLY_LIMIT,
    NOW,
    Rig,
    ScriptedOrgans,
)

pytestmark = pytest.mark.asyncio

EVENT_HORMONE_TRIGGERED = "hormone_triggered"


class Env:
    """同一个数据库文件上先后建两个心跳，模拟重启。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = ManualClock(NOW)
        self.opened: list[Rig] = []
        self.people: list[PeopleDirectory] = []

    def build(
        self, judge: list[str | BaseException], *, enabled: bool = True
    ) -> tuple[Rig, Hormones | None]:
        db_path = self.tmp_path / "aureus.db"
        ledger = Ledger(db_path, self.clock, calendar=CALENDAR)
        timeline = TimelineDatabase(db_path, self.clock)
        people = PeopleDirectory(db_path, self.clock)
        for opened in (ledger, timeline, people):
            opened.open()
        people.ensure_user("阿明", [(CHANNEL, "local")])
        stop = EmergencyStop(self.tmp_path / "halt.json", self.clock)
        organs = ScriptedOrgans(judge)
        hormones = Hormones(HormonesConfig(enabled=True)) if enabled else None
        beating = Heartbeat(
            clock=self.clock,
            timeline=timeline,
            organs=organs,
            identity=Identity.model_validate(
                {
                    "facts": {"name": "小绿", "birthday": "2008-01-01"},
                    "user": {"name": "阿明"},
                    "persona": {"immutable": "温和。", "behavior_style": "少打断。"},
                }
            ),
            config=HeartbeatConfig(),
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            timezone=UTC,
            body=Body(BodyConfig(), MONTHLY_LIMIT, ledger, self.clock, CALENDAR),
            judge=JudgeConfig(enabled=True),
            people=people,
            hormones=hormones,
        )
        rig = Rig(ledger, timeline, organs, beating)
        beating.attach_channel(CHANNEL, rig.sent.append)
        beating.wake_up()
        self.opened.append(rig)
        self.people.append(people)
        return rig, hormones

    def close(self) -> None:
        for rig in self.opened:
            rig.ledger.close()
            rig.timeline.close()
        for people in self.people:
            people.close()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    yield environment
    environment.close()


async def test_a_judged_offense_raises_anger_and_is_recorded(env: Env) -> None:
    rig, hormones = env.build([GOOD_JUDGMENT])
    assert hormones is not None
    await rig.exchange("你这个破 AI")

    assert hormones.level(HormoneKind.ANGER, env.clock.now()) > 0
    [record] = rig.records(EVENT_HORMONE_TRIGGERED)
    assert record.visibility is VisibilityScope.PRIVATE
    assert record.metadata["kind"] == "anger"
    assert isinstance(record.metadata["person_id"], int)


async def test_no_hormones_means_no_trigger_records(env: Env) -> None:
    rig, hormones = env.build([GOOD_JUDGMENT], enabled=False)
    assert hormones is None
    await rig.exchange("你这个破 AI")

    assert rig.records(EVENT_HORMONE_TRIGGERED) == []


async def test_a_calm_judgment_triggers_nothing(env: Env) -> None:
    rig, hormones = env.build(['{"others": [], "toward_me": []}'])
    assert hormones is not None
    await rig.exchange("你好")

    assert rig.records(EVENT_HORMONE_TRIGGERED) == []
    assert hormones.level(HormoneKind.ANGER, env.clock.now()) == 0


async def test_levels_are_restored_after_a_restart_and_fade_with_downtime(env: Env) -> None:
    rig, _ = env.build([GOOD_JUDGMENT])
    await rig.exchange("你这个破 AI")
    before = len(rig.records(EVENT_HORMONE_TRIGGERED))
    assert before == 1
    await rig.heartbeat.shut_down()
    env.clock.advance(timedelta(minutes=5))

    _, restored = env.build([])
    assert restored is not None
    assert restored.level(HormoneKind.ANGER, env.clock.now()) > 0

    env.clock.advance(timedelta(days=30))
    _, faded = env.build([])
    assert faded is not None
    assert faded.level(HormoneKind.ANGER, env.clock.now()) == 0
    # 还原不会重复写记录。
    rows = TimelineQuery(event_type=EVENT_HORMONE_TRIGGERED, limit=100)
    assert len(list(env.opened[-1].timeline.query(rows))) == before
