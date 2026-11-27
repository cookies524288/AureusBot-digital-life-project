"""同理心接进心跳的测试（路线图 2.5，D72）：别人难过它也难过，好感高的人引起的更强。"""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from aureus.attention import WorkingMemory
from aureus.body import Body
from aureus.common import (
    BodyConfig,
    EmpathyConfig,
    HeartbeatConfig,
    HormonesConfig,
    JudgeConfig,
    ManualClock,
    MoodConfig,
    PeopleConfig,
)
from aureus.heartbeat import Heartbeat, Perception, TrustLevel
from aureus.heartbeat.events import EVENT_EMPATHY, EVENT_HORMONE_TRIGGERED
from aureus.hormones import HormoneKind, Hormones
from aureus.identity import Identity
from aureus.mood import Mood
from aureus.people import AffinityBook, PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope
from tests.heartbeat.test_judging_flow import (
    CALENDAR,
    CHANNEL,
    MONTHLY_LIMIT,
    NOW,
    GatedJudge,
    Rig,
    ScriptedOrgans,
)

pytestmark = pytest.mark.asyncio


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
        config: HeartbeatConfig | None = None,
        organs: ScriptedOrgans | None = None,
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
        organs = organs or ScriptedOrgans(judge)
        hormones = Hormones(HormonesConfig(enabled=True))
        config = config or HeartbeatConfig()
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
            config=config,
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            timezone=UTC,
            body=Body(
                BodyConfig(stress_enabled=False), MONTHLY_LIMIT, ledger, self.clock, CALENDAR
            ),
            judge=JudgeConfig(enabled=True),
            people=people,
            hormones=hormones,
            mood=Mood(MoodConfig()),
            affinity=affinity,
            empathy=empathy if empathy is not None else EmpathyConfig(),
            working_memory=WorkingMemory(timeline, config),
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
        await rig.heartbeat.beat()
        await rig.heartbeat.wait_for_thought()
        await rig.heartbeat.beat()
        await rig.settle_judgments()

    def close(self) -> None:
        for item in reversed(self.closers):
            item.close()  # type: ignore[attr-defined]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    yield environment
    environment.close()


async def _hurt_after_one_sad_message(env: Env, *, stranger: bool) -> float:
    rig, _, _ = env.build([SAD], stranger=stranger)
    await env.say(rig, "我今天被裁员了")
    [record] = rig.records(EVENT_EMPATHY)
    return float(record.metadata["rise"])


async def test_sad_message_makes_it_sad_and_says_why(env: Env) -> None:
    """D110：判断与规划并行，本轮回复用启动时快照；共情从下一轮才进入回复上下文。"""
    calm = '{"others": [], "toward_me": []}'
    rig, hormones, _ = env.build([SAD, calm])
    await env.say(rig, "我今天被裁员了")

    assert hormones.level(HormoneKind.HURT, env.clock.now()) == 0
    assert "低落 0.25" in rig.heartbeat.describe_state()
    [record] = rig.records(EVENT_EMPATHY)
    assert rig.records(EVENT_HORMONE_TRIGGERED) == []
    assert record.metadata["kind"] == "hurt"
    assert record.metadata["empathy_for"] == "sender"
    assert record.metadata["empathy_affinity"] == 80.0
    assert record.metadata["reason"] == "替对方难过"
    replyer = [r for r in rig.organs.requests if r.organ_id == "replyer"][-1]
    # 按 D110 改写：原来断言同一轮回复就带上共情和心情，现在只等下一轮。
    assert "替对方难过" not in replyer.system_context
    assert "低落" not in replyer.system_context
    assert "心情 -0.13" not in replyer.system_context

    await env.say(rig, "还是很难过")
    replyer = [r for r in rig.organs.requests if r.organ_id == "replyer"][-1]
    assert "低落" in replyer.system_context and "替对方难过" in replyer.system_context
    assert "心情 -0.13" in replyer.system_context


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
    rig, _, _ = env.build([SAD] * 10)
    for index in range(10):
        await env.say(rig, f"好难过 {index}")
    rises = [r.metadata["rise"] for r in rig.records(EVENT_EMPATHY)]
    assert rises == sorted(rises, reverse=True) and rises[-1] < rises[0]
    assert "低落 0.00" in rig.heartbeat.describe_state()


async def test_mentioned_animal_uses_the_stranger_default_affinity(env: Env) -> None:
    rig, _, _ = env.build([SAD_CAT])
    await env.say(rig, "我家猫小橘病了，蔫蔫的")
    [record] = rig.records(EVENT_EMPATHY)
    assert record.metadata["empathy_for"] == "小橘"
    assert record.metadata["empathy_affinity"] == PeopleConfig().stranger_affinity
    assert record.metadata["reason"] == "替小橘难过"


async def test_unmentioned_animal_does_not_contribute_empathy(env: Env) -> None:
    rig, hormones, _ = env.build([SAD_CAT])
    await env.say(rig, "我家猫病了，蔫蔫的")

    assert any(request.organ_id == "judge" for request in rig.organs.requests)
    assert rig.records(EVENT_EMPATHY) == []
    assert hormones.level(HormoneKind.HURT, env.clock.now()) == 0
    assert "低落 0.00" in rig.heartbeat.describe_state()


async def test_empathic_anger_is_felt_but_does_not_start_a_fight(env: Env) -> None:
    rig, _, _ = env.build([ANGRY_FOR_THEM])
    await env.say(rig, "老板今天当众骂我")

    assert "怒气 0.25" in rig.heartbeat.describe_state()
    replyer = [r for r in rig.organs.requests if r.organ_id == "replyer"][-1]
    assert "允许质疑" not in replyer.system_context


async def test_switching_empathy_off_keeps_the_judge_and_affinity_working(env: Env) -> None:
    rig, hormones, affinity = env.build([SAD], empathy=EmpathyConfig(enabled=False))
    await env.say(rig, "我今天被裁员了")

    assert [r.organ_id for r in rig.organs.requests].count("judge") == 1
    assert rig.records(EVENT_EMPATHY) == []
    assert hormones.level(HormoneKind.HURT, env.clock.now()) == 0
    assert affinity is not None and len(affinity.history(1)) >= 1  # 互动量照常记


async def test_failed_judgment_means_no_empathy(env: Env) -> None:
    rig, hormones, _ = env.build(["完全不是 JSON"])
    await env.say(rig, "我今天被裁员了")
    assert rig.records(EVENT_EMPATHY) == []
    assert hormones.level(HormoneKind.HURT, env.clock.now()) == 0
    assert len(rig.replies()) == 1


async def test_without_an_affinity_book_empathy_uses_the_stranger_default(env: Env) -> None:
    rig, _, _ = env.build([SAD], with_affinity=False)
    await env.say(rig, "我今天被裁员了")
    [record] = rig.records(EVENT_EMPATHY)
    assert record.metadata["empathy_affinity"] == PeopleConfig().stranger_affinity


async def test_retention_drops_only_the_expired_source_after_decay_and_restart(env: Env) -> None:
    config = HeartbeatConfig(working_memory_capacity=20, working_memory_retain=5)
    direct = '{"kind":"rejection","intensity":1,"confidence":1}'
    first = SAD.replace('"toward_me": []', f'"toward_me": [{direct}]')
    rig, hormones, _ = env.build([first, SAD_CAT], config=config)
    await env.say(rig, "我很难过")
    await env.say(rig, "小橘也很难过")
    [first_effect, second_effect] = rig.records(EVENT_EMPATHY)
    assert first_effect.metadata["source_record_id"] != second_effect.metadata["source_record_id"]
    [baseline] = rig.records(EVENT_HORMONE_TRIGGERED)
    assert "empathy_for" not in baseline.metadata
    assert "低落 0.73" in rig.heartbeat.describe_state()

    env.clock.advance(timedelta(seconds=hormones.config.hurt_half_life_seconds))
    assert "低落 0.37" in rig.heartbeat.describe_state()
    recovered, _, _ = env.build([], config=config)
    assert "低落 0.37" in recovered.heartbeat.describe_state()

    recovered.timeline.append("agenda_added", "焦点更新", ActorType.ME)
    assert "低落 0.24" in recovered.heartbeat.describe_state()
    recovered.timeline.append("agenda_added", "另一条经历", ActorType.ME)
    recovered.timeline.append("agenda_added", "再一条经历", ActorType.ME)
    assert "低落 0.20" in recovered.heartbeat.describe_state()
    assert len(recovered.records(EVENT_EMPATHY)) == 2


async def test_working_memory_switch_and_legacy_empathy_do_not_restore_baseline(env: Env) -> None:
    rig, _, _ = env.build([SAD])
    await env.say(rig, "我好难过")
    [current] = rig.records(EVENT_EMPATHY)
    rig.timeline.append(
        EVENT_HORMONE_TRIGGERED, "旧共情留痕", ActorType.ME, metadata=current.metadata
    )

    no_empathy, _, _ = env.build([], empathy=EmpathyConfig(enabled=False))
    assert "低落 0.00" in no_empathy.heartbeat.describe_state()
    assert "心情 +0.00" in no_empathy.heartbeat.describe_state()
    no_memory, _, _ = env.build([], config=HeartbeatConfig(working_memory_enabled=False))
    assert "低落 0.00" in no_memory.heartbeat.describe_state()
    assert "心情 +0.00" in no_memory.heartbeat.describe_state()


async def test_same_target_counts_once_and_unknown_sources_are_ignored(env: Env) -> None:
    duplicate = json.loads(SAD)
    duplicate["others"].extend(
        [
            duplicate["others"][0],
            {**duplicate["others"][0], "source_record_id": 999},
            {**duplicate["others"][0], "subject": "不在来信里的人"},
        ]
    )
    rig, _, _ = env.build([json.dumps(duplicate), SAD])
    await env.say(rig, "我好难过")
    assert len(rig.records(EVENT_EMPATHY)) == 1
    assert "低落 0.25" in rig.heartbeat.describe_state()
    await env.say(rig, "现在还是很难过")
    assert len(rig.records(EVENT_EMPATHY)) == 2
    assert "低落 0.15" in rig.heartbeat.describe_state()


async def test_batched_judgment_uses_the_explicit_source_and_not_the_last_message(env: Env) -> None:
    rig, _, _ = env.build([])
    assert rig.organs.judge is not None
    source_id = rig.timeline.get_latest_id() + 1
    report = json.loads(SAD_CAT)
    report["others"][0]["source_record_id"] = source_id
    report["others"].append(json.loads(SAD)["others"][0])
    rig.organs.judge.append(json.dumps(report, ensure_ascii=False))
    for text in ("小橘今天好难过", "我刚刚去买了东西"):
        assert rig.heartbeat.submit(
            Perception(
                channel=CHANNEL,
                sender_id="local",
                content=text,
                visibility=VisibilityScope.USER_ONLY,
                trust=TrustLevel.VERIFIED_USER,
            )
        )
    await rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()
    await rig.settle_judgments()

    [record] = rig.records(EVENT_EMPATHY)
    assert record.metadata["source_record_id"] == source_id
    assert record.metadata["empathy_for"] == "小橘"


async def test_late_empathy_is_dropped_when_the_source_left_the_focus(env: Env) -> None:
    """D110：判断晚到时来源已经离开工作记忆保留区，就不能再添回一份共情。"""
    config = HeartbeatConfig(working_memory_capacity=20, working_memory_retain=5)
    gate, started = asyncio.Event(), asyncio.Event()
    organs = GatedJudge([SAD], gate, started)
    rig, hormones, _ = env.build([], config=config, organs=organs)

    assert rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local",
            content="我今天被裁员了",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    await rig.heartbeat.beat()
    await asyncio.wait_for(started.wait(), timeout=5)
    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()
    assert len(rig.replies()) == 1

    for index in range(6):
        rig.timeline.append("agenda_added", f"后来的经历 {index}", ActorType.ME)

    gate.set()
    await rig.settle_judgments()

    assert rig.records(EVENT_EMPATHY) == []
    assert hormones.level(HormoneKind.HURT, env.clock.now()) == 0
