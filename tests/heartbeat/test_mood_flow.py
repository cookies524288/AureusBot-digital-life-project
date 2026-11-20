"""心情接进心跳的测试（路线图 2.6 验收，D73）。

连续失败后心情变低、上下文里有谨慎说明和原因；成功加时间让它恢复；被夸心情变好；
重启后心情和没重启一样；开关关掉后不进上下文。
"""

from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import (
    BodyConfig,
    FeedbackConfig,
    HeartbeatConfig,
    HormonesConfig,
    JudgeConfig,
    ManualClock,
    MoodConfig,
)
from aureus.feedback import FeedbackStore, Outcome
from aureus.heartbeat import Heartbeat
from aureus.hormones import Hormones
from aureus.identity import Identity
from aureus.mood import Mood
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase
from tests.heartbeat.test_judging_flow import (
    CALENDAR,
    CHANNEL,
    MONTHLY_LIMIT,
    NOW,
    Rig,
    ScriptedOrgans,
)

pytestmark = pytest.mark.asyncio

TASK = "sim_task:hard"
PRAISE = '{"others": [], "toward_me": [{"kind": "praise", "intensity": 1.0, "confidence": 1.0}]}'
NOTHING = '{"others": [], "toward_me": []}'
HALF_LIFE = timedelta(seconds=MoodConfig().half_life_seconds)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = ManualClock(NOW)
        self.closers: list[object] = []
        self.feedback: FeedbackStore | None = None

    def build(
        self, judge: list[str | BaseException] | None = None, *, mood_enabled: bool = True
    ) -> tuple[Rig, Mood | None]:
        db_path = self.tmp_path / "aureus.db"
        ledger = Ledger(db_path, self.clock, calendar=CALENDAR)
        timeline = TimelineDatabase(db_path, self.clock)
        people = PeopleDirectory(db_path, self.clock)
        feedback = FeedbackStore(db_path, FeedbackConfig(), self.clock)
        for opened in (ledger, timeline, people, feedback):
            opened.open()
            self.closers.append(opened)
        self.feedback = feedback
        people.ensure_user("阿明", [(CHANNEL, "local")])
        stop = EmergencyStop(self.tmp_path / "halt.json", self.clock)
        mood = Mood(MoodConfig()) if mood_enabled else None
        organs = ScriptedOrgans(judge)
        beating = Heartbeat(
            clock=self.clock,
            timeline=timeline,
            organs=organs,
            identity=Identity.model_validate(
                {
                    "facts": {"name": "小绿", "birthday": "2008-01-01"},
                    "user": {"name": "阿明"},
                    "persona": {"immutable": "温和。"},
                }
            ),
            config=HeartbeatConfig(),
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            timezone=UTC,
            body=Body(BodyConfig(), MONTHLY_LIMIT, ledger, self.clock, CALENDAR),
            judge=JudgeConfig(enabled=judge is not None),
            people=people,
            feedback=feedback,
            hormones=Hormones(HormonesConfig(enabled=True)),
            mood=mood,
        )
        rig = Rig(ledger, timeline, organs, beating)
        beating.attach_channel(CHANNEL, rig.sent.append)
        beating.wake_up()
        return rig, mood

    def tasks(self, outcome: Outcome, count: int) -> None:
        assert self.feedback is not None
        for _ in range(count):
            self.feedback.record_outcome(self.feedback.record_expectation(TASK, 0.5), outcome)

    def close(self) -> None:
        for item in reversed(self.closers):
            item.close()  # type: ignore[attr-defined]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    yield environment
    environment.close()


def _context(rig: Rig) -> str:
    return [r for r in rig.organs.requests if r.organ_id == "replyer"][-1].system_context


async def test_failures_make_it_cautious_and_success_with_time_recovers(env: Env) -> None:
    rig, mood = env.build()
    assert mood is not None
    env.tasks(Outcome.FAILURE, 5)

    await rig.exchange("今天怎么样")
    context = _context(rig)
    assert "【心情】" in context and "谨慎" in context
    assert f"{TASK} 失败了" in context
    assert mood.cautious
    assert "谨慎中" in rig.heartbeat.describe_state()

    env.clock.advance(HALF_LIFE)
    env.tasks(Outcome.SUCCESS, 4)
    await rig.exchange("现在呢")
    assert "谨慎状态" not in _context(rig)
    assert not mood.cautious


async def test_praise_lifts_the_mood(env: Env) -> None:
    rig, mood = env.build([PRAISE, NOTHING])
    assert mood is not None
    await rig.exchange("你真厉害")
    await rig.exchange("嗯")
    assert mood.read(env.clock.now()).valence > 0
    assert "被夸了" in _context(rig)


async def test_restart_keeps_the_mood(env: Env) -> None:
    rig, mood = env.build([PRAISE])
    assert mood is not None
    await rig.exchange("你真厉害")
    before = mood.read(env.clock.now() + timedelta(hours=2)).valence
    await rig.heartbeat.shut_down()

    env.clock.advance(timedelta(hours=2))
    _, restored = env.build([])
    assert restored is not None
    assert restored.read(env.clock.now()).valence == pytest.approx(before)
    assert before > 0


async def test_switched_off_mood_stays_out_of_the_context(env: Env) -> None:
    rig, mood = env.build(mood_enabled=False)
    assert mood is None
    env.tasks(Outcome.FAILURE, 5)
    await rig.exchange("今天怎么样")
    assert "【心情】" not in _context(rig)
    assert "心情" not in rig.heartbeat.describe_state()
