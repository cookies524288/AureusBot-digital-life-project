"""同理心接进心跳的测试（路线图 2.5，D72）：别人难过它也难过，好感高的人引起的更强。"""

from collections.abc import Iterator
from datetime import UTC
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import (
    BodyConfig,
    EmpathyConfig,
    HeartbeatConfig,
    HormonesConfig,
    JudgeConfig,
    ManualClock,
    PeopleConfig,
)
from aureus.heartbeat import Heartbeat, Perception, TrustLevel
from aureus.hormones import HormoneKind, Hormones
from aureus.identity import Identity
from aureus.people import AffinityBook, PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase, VisibilityScope
from tests.heartbeat.test_judging_flow import (
    CALENDAR,
    CHANNEL,
    MONTHLY_LIMIT,
    NOW,
    Rig,
    ScriptedOrgans,
)

pytestmark = pytest.mark.asyncio

EVENT_HORMONE_TRIGGERED = "hormone_triggered"


def _others(subject: str, emotion: str) -> str:
    item = (
        f'{{"subject": "{subject}", "emotion": "{emotion}", "intensity": 1.0, "confidence": 1.0}}'
    )
    return f'{{"others": [{item}], "toward_me": []}}'


SAD = _others("sender", "hurt")
SAD_CAT = _others("小橘", "hurt")
ANGRY_FOR_THEM = _others("sender", "anger")


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = ManualClock(NOW)
        self.closers: list[object] = []

    def build(
        self,
        judge: list[str | BaseException],
        *,
        stranger: bool = False,
        empathy: EmpathyConfig | None = None,
        with_affinity: bool = True,
    ) -> tuple[Rig, Hormones, AffinityBook | None]:
        """stranger=True 时说话的是一个陌生人（好感 30），否则是用户（好感 80）。"""
        db_path = self.tmp_path / "aureus.db"
        ledger = Ledger(db_path, self.clock, calendar=CALENDAR)
        timeline = TimelineDatabase(db_path, self.clock)
        people = PeopleDirectory(db_path, self.clock)
        for opened in (ledger, timeline, people):
            opened.open()
            self.closers.append(opened)
        people.ensure_user("阿明", [(CHANNEL, "local")])
        affinity = None
        if with_affinity:
            affinity = AffinityBook(
                db_path, self.clock, PeopleConfig(), CALENDAR, min_confidence=0.3
            )
            affinity.open()
            self.closers.append(affinity)
        stop = EmergencyStop(self.tmp_path / "halt.json", self.clock)
        organs = ScriptedOrgans(judge)
        hormones = Hormones(HormonesConfig(enabled=True))
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
            judge=JudgeConfig(enabled=True),
            people=people,
            hormones=hormones,
            affinity=affinity,
            empathy=empathy if empathy is not None else EmpathyConfig(),
        )
        rig = Rig(ledger, timeline, organs, beating)
        beating.attach_channel(CHANNEL, rig.sent.append)
        beating.wake_up()
        self.speaker = "stranger" if stranger else "local"
        self.trust = TrustLevel.VERIFIED_CONTACT if stranger else TrustLevel.VERIFIED_USER
        return rig, hormones, affinity

    async def say(self, rig: Rig, text: str) -> None:
        rig.heartbeat.submit(
            Perception(
                channel=CHANNEL,
                sender_id=self.speaker,
                content=text,
                visibility=VisibilityScope.USER_ONLY,
                trust=self.trust,
            )
        )
        rig.heartbeat.beat()
        await rig.heartbeat.wait_for_thought()
        rig.heartbeat.beat()

    def close(self) -> None:
        for item in reversed(self.closers):
            item.close()  # type: ignore[attr-defined]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    yield environment
    environment.close()


async def _hurt_after_one_sad_message(env: Env, *, stranger: bool) -> float:
    rig, hormones, _ = env.build([SAD], stranger=stranger)
    await env.say(rig, "我今天被裁员了")
    return hormones.level(HormoneKind.HURT, env.clock.now())


async def test_sad_message_makes_it_sad_and_says_why(env: Env) -> None:
    rig, hormones, _ = env.build([SAD])
    await env.say(rig, "我今天被裁员了")

    assert hormones.level(HormoneKind.HURT, env.clock.now()) > 0
    [record] = rig.records(EVENT_HORMONE_TRIGGERED)
    assert record.metadata["kind"] == "hurt"
    assert record.metadata["empathy_for"] == "sender"
    assert record.metadata["empathy_affinity"] == 80.0
    assert record.metadata["reason"] == "替对方难过"
    replyer = [r for r in rig.organs.requests if r.organ_id == "replyer"][-1]
    assert "低落" in replyer.system_context and "替对方难过" in replyer.system_context


async def test_higher_affinity_makes_it_feel_more_than_a_stranger(tmp_path: Path) -> None:
    close = Env(tmp_path / "close")
    far = Env(tmp_path / "far")
    try:
        close_level = await _hurt_after_one_sad_message(close, stranger=False)
        far_level = await _hurt_after_one_sad_message(far, stranger=True)
    finally:
        close.close()
        far.close()
    assert close_level > far_level > 0


async def test_spamming_sadness_cannot_flood_the_mood(env: Env) -> None:
    rig, hormones, _ = env.build([SAD] * 10)
    for index in range(10):
        await env.say(rig, f"好难过 {index}")
    rises = [r.metadata["rise"] for r in rig.records(EVENT_HORMONE_TRIGGERED)]
    assert rises == sorted(rises, reverse=True) and rises[-1] < rises[0]
    assert hormones.level(HormoneKind.HURT, env.clock.now()) <= 1.0


async def test_mentioned_animal_uses_the_stranger_default_affinity(env: Env) -> None:
    rig, _, _ = env.build([SAD_CAT])
    await env.say(rig, "我家猫病了，蔫蔫的")
    [record] = rig.records(EVENT_HORMONE_TRIGGERED)
    assert record.metadata["empathy_for"] == "小橘"
    assert record.metadata["empathy_affinity"] == PeopleConfig().stranger_affinity
    assert record.metadata["reason"] == "替小橘难过"


async def test_empathic_anger_is_felt_but_does_not_start_a_fight(env: Env) -> None:
    rig, hormones, _ = env.build([ANGRY_FOR_THEM])
    await env.say(rig, "老板今天当众骂我")

    assert hormones.level(HormoneKind.ANGER, env.clock.now()) > 0
    replyer = [r for r in rig.organs.requests if r.organ_id == "replyer"][-1]
    assert "允许质疑" not in replyer.system_context


async def test_switching_empathy_off_keeps_the_judge_and_affinity_working(env: Env) -> None:
    rig, hormones, affinity = env.build([SAD], empathy=EmpathyConfig(enabled=False))
    await env.say(rig, "我今天被裁员了")

    assert [r.organ_id for r in rig.organs.requests].count("judge") == 1
    assert rig.records(EVENT_HORMONE_TRIGGERED) == []
    assert hormones.level(HormoneKind.HURT, env.clock.now()) == 0
    assert affinity is not None and len(affinity.history(1)) >= 1  # 互动量照常记


async def test_failed_judgment_means_no_empathy(env: Env) -> None:
    rig, hormones, _ = env.build(["完全不是 JSON"])
    await env.say(rig, "我今天被裁员了")
    assert rig.records(EVENT_HORMONE_TRIGGERED) == []
    assert hormones.level(HormoneKind.HURT, env.clock.now()) == 0
    assert len(rig.replies()) == 1


async def test_without_an_affinity_book_empathy_uses_the_stranger_default(env: Env) -> None:
    rig, _, _ = env.build([SAD], with_affinity=False)
    await env.say(rig, "我今天被裁员了")
    [record] = rig.records(EVENT_HORMONE_TRIGGERED)
    assert record.metadata["empathy_affinity"] == PeopleConfig().stranger_affinity
