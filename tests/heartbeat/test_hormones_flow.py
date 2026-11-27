"""激素接进心跳的测试（路线图 2.3 步骤 4a，D69）：判断结果变成触发，记进时间线，重启后还原。"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import (
    BodyConfig,
    HeartbeatConfig,
    HormonesConfig,
    JudgeConfig,
    ManualClock,
    MoodConfig,
)
from aureus.heartbeat import Heartbeat, MessageRoute, Perception, TrustLevel
from aureus.hormones import HormoneKind, Hormones
from aureus.identity import Identity
from aureus.mood import Mood
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase, TimelineQuery, VisibilityScope
from tests.heartbeat import test_judging_flow
from tests.heartbeat.test_judging_flow import (
    CALENDAR,
    CHANNEL,
    GOOD_JUDGMENT,
    MONTHLY_LIMIT,
    NOW,
    GatedJudge,
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
        self,
        judge: list[str | BaseException],
        *,
        enabled: bool = True,
        state_enabled: bool = True,
        hormone_config: HormonesConfig | None = None,
        body_config: BodyConfig | None = None,
        with_mood: bool = False,
        roleplay: bool = False,
        organs: ScriptedOrgans | None = None,
    ) -> tuple[Rig, Hormones | None]:
        db_path = self.tmp_path / "aureus.db"
        ledger = Ledger(db_path, self.clock, calendar=CALENDAR)
        timeline = TimelineDatabase(db_path, self.clock)
        people = PeopleDirectory(db_path, self.clock)
        for opened in (ledger, timeline, people):
            opened.open()
        people.ensure_user("阿明", [(CHANNEL, "local")])
        stop = EmergencyStop(self.tmp_path / "halt.json", self.clock)
        organs = organs or ScriptedOrgans(judge)
        hormones = Hormones(hormone_config or HormonesConfig()) if enabled else None
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
            config=HeartbeatConfig(roleplay_mode=roleplay),
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            timezone=UTC,
            body=Body(body_config or BodyConfig(), MONTHLY_LIMIT, ledger, self.clock, CALENDAR),
            judge=JudgeConfig(state_context_enabled=state_enabled),
            people=people,
            hormones=hormones,
            mood=Mood(MoodConfig()) if with_mood else None,
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


@pytest.mark.parametrize("state_enabled", [True, False])
@pytest.mark.parametrize("valid", [True, False])
async def test_judgment_affects_only_the_next_round(
    env: Env, monkeypatch: pytest.MonkeyPatch, *, state_enabled: bool, valid: bool
) -> None:
    """D110：判断与规划并行，规划和回复用启动时的快照；判断结果从下一轮才进入上下文。

    原来这里断言同一轮的规划器和回复器立刻带上判断引起的低落，现按 D110 改为：
    同一轮不带，判断被采纳后下一轮才带。
    """
    monkeypatch.setattr(test_judging_flow, "PLAN", test_judging_flow.PLAN.replace('"短"', '"长"'))
    confidence = 1 if valid else 2
    judgment = (
        '{"others": [], "toward_me": [{"kind": "rejection", '
        f'"intensity": 1, "confidence": {confidence}' + "}]}"
    )
    calm = '{"others": [], "toward_me": []}'
    rig, _ = env.build(
        [judgment, calm],
        state_enabled=state_enabled,
        hormone_config=HormonesConfig(hurt_shorten_at=0.3, joy_enabled=False),
        body_config=BodyConfig(stress_enabled=False),
        with_mood=True,
    )
    await rig.exchange("我不想理你了")

    judge = next(request for request in rig.organs.requests if request.organ_id == "judge")
    planner = next(request for request in rig.organs.requests if request.organ_id == "planner")
    replyer = next(request for request in rig.organs.requests if request.organ_id == "replyer")
    if state_enabled:
        assert "【判断前的自身状态】" in judge.system_context
        assert "精力 100%" in judge.system_context
        assert "低落 0.00" in judge.system_context
        assert "心情（−1 至 1）：+0.00" in judge.system_context
        assert "不因自己的状态推断对方" in judge.system_context
        assert "愉悦" not in judge.system_context and "压力" not in judge.system_context
    else:
        assert judge.system_context == "【不变人格】\n温和。"
    for request in (planner, replyer):
        assert "低落 0.40" not in request.system_context
        assert "心情 -0.20" not in request.system_context
        assert "回复长度上限调整为：三五句话" not in request.system_context
    assert "三五句话" not in replyer.prompt
    assert len(rig.records(EVENT_HORMONE_TRIGGERED)) == int(valid)

    await rig.exchange("好吧")
    planner = [request for request in rig.organs.requests if request.organ_id == "planner"][-1]
    replyer = [request for request in rig.organs.requests if request.organ_id == "replyer"][-1]
    for request in (planner, replyer):
        assert ("低落 0.40" in request.system_context) is valid
        assert ("心情 -0.20" in request.system_context) is valid
        assert ("回复长度上限调整为：三五句话" in request.system_context) is valid
    assert ("三五句话" in replyer.prompt) is valid
    assert len(rig.records(EVENT_HORMONE_TRIGGERED)) == int(valid)


async def test_blocked_judgment_does_not_hold_the_reply_and_is_adopted_once(env: Env) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    calm = '{"others": [], "toward_me": []}'
    organs = GatedJudge([GOOD_JUDGMENT, calm, calm], gate, started)
    rig, hormones = env.build([], organs=organs)
    assert hormones is not None

    async def say(text: str) -> None:
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

    await say("你这个破 AI")
    await asyncio.wait_for(started.wait(), timeout=5)
    # 判断还卡着，正文已经投递；本轮激素不变。
    assert rig.replies() == ["好的。"]
    assert rig.records(EVENT_HORMONE_TRIGGERED) == []
    assert hormones.level(HormoneKind.ANGER, env.clock.now()) == 0

    await say("还在吗")
    planner = [request for request in organs.requests if request.organ_id == "planner"][-1]
    assert "怒气" not in planner.system_context
    assert rig.records(EVENT_HORMONE_TRIGGERED) == []

    # 判断卡住的这段时间时钟继续走：采纳时按当时的时钟记触发，而不是按判断出发时。
    env.clock.advance(timedelta(seconds=90))
    gate.set()
    await rig.settle_judgments()
    [trigger] = rig.records(EVENT_HORMONE_TRIGGERED)
    assert trigger.metadata["kind"] == "anger"
    assert trigger.metadata["triggered_at"] == (NOW + timedelta(seconds=90)).isoformat()
    first_message = next(
        record for record in rig.records("message") if record.actor_type.value == "user"
    )
    # 触发来源仍指原来的那条消息，而不是晚到时的哪条新消息。
    assert trigger.metadata["source_record_id"] == first_message.id
    assert hormones.level(HormoneKind.ANGER, env.clock.now()) > 0

    await say("你怎么不说话")
    planner = [request for request in organs.requests if request.organ_id == "planner"][-1]
    assert "怒气" in planner.system_context
    await rig.settle_judgments()
    # 同一个判断只采纳一次。
    assert len(rig.records(EVENT_HORMONE_TRIGGERED)) == 1


async def test_judgment_finishing_first_still_does_not_change_the_current_reply(env: Env) -> None:
    """反向顺序：判断先完成也不改写已经出发的规划，结果仍只留给后续轮次。"""
    gate, started = asyncio.Event(), asyncio.Event()
    organs = GatedJudge([GOOD_JUDGMENT], gate, started)
    rig, hormones = env.build([], organs=organs)
    assert hormones is not None
    assert rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local",
            content="你这个破 AI",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    await rig.heartbeat.beat()
    await asyncio.wait_for(started.wait(), timeout=5)

    gate.set()
    [pending] = rig.heartbeat._pending_judgments
    await asyncio.wait({pending.task}, timeout=5)
    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()

    [replyer] = [request for request in organs.requests if request.organ_id == "replyer"]
    assert "怒气" not in replyer.system_context
    assert hormones.level(HormoneKind.ANGER, env.clock.now()) > 0


async def test_judge_does_not_reuse_private_or_other_group_evidence(env: Env) -> None:
    calm = '{"others": [], "toward_me": []}'
    rig, _ = env.build([GOOD_JUDGMENT, calm, calm, calm], with_mood=True)
    await rig.exchange("私聊证据只出现在终端")
    for group, index, content in (
        ("a", "1", "本群先前消息"),
        ("b", "2", "另一群的消息"),
        ("a", "3", "本群当前消息"),
    ):
        assert rig.heartbeat.submit(
            Perception(
                CHANNEL,
                "group-member",
                content,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                MessageRoute("bot", "group", group, index),
                mentioned=True,
            )
        )
        await rig.heartbeat.beat()
        await rig.heartbeat.wait_for_thought()
        await rig.heartbeat.beat()
        await rig.settle_judgments()

    _, _, _, group_judge = [item for item in rig.organs.requests if item.organ_id == "judge"]
    assert "本群先前消息" in group_judge.prompt
    assert "本群当前消息" in group_judge.prompt
    assert "另一群的消息" not in group_judge.prompt
    assert "私聊证据" not in group_judge.prompt + group_judge.system_context
    assert "阿明" not in group_judge.system_context
    assert "精力" not in group_judge.system_context and "社交需求" not in group_judge.system_context
    assert "情绪（0 至 1）" in group_judge.system_context


async def test_roleplay_does_not_send_state_to_judge(env: Env) -> None:
    rig, _ = env.build([GOOD_JUDGMENT], roleplay=True, with_mood=True)
    await rig.exchange("你好吗")
    [judge] = [request for request in rig.organs.requests if request.organ_id == "judge"]
    assert judge.system_context == "【不变人格】\n温和。"


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
