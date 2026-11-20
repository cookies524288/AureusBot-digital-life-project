"""激素影响接进心跳回复的测试（路线图 2.3 步骤 4b，D69）：回击、冷处理、低落缩短、惊讶。"""

from collections.abc import Iterator
from datetime import UTC
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, HeartbeatConfig, HormonesConfig, JudgeConfig, ManualClock
from aureus.heartbeat import Heartbeat
from aureus.hormones import HormoneKind, Hormones, HormoneTrigger
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
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

EVENT_MESSAGE = "message"
EVENT_REPLY_DECLINED = "reply_declined"
# 一次冒犯最多涨 0.4，默认的回击线 0.5 一次够不到，测试把线调低。
ANGRY = HormonesConfig(enabled=True, retaliate_at=0.25, cold_shoulder_at=0.3)
DECLINE = '{"reply": false, "intent": "", "length": "短", "reason": "气得不想理"}'
REJECTION = (
    '{"others": [], "toward_me": [{"kind": "rejection", "intensity": 1.0, "confidence": 0.9}]}'
)


def _judgment(intensity: float) -> str:
    """冒犯，强度可调（0–1）。"""
    return (
        '{"others": [], "toward_me": [{"kind": "offense", "intensity": '
        f"{intensity}"
        ', "confidence": 0.9}]}'
    )


class PlanOrgans(ScriptedOrgans):
    """规划器按预设依次给计划，用完后用默认的；其余照 ScriptedOrgans。"""

    def __init__(self, judge: list[str | BaseException] | None, plans: list[str]) -> None:
        super().__init__(judge)
        self.plans = plans

    async def call(self, request: OrganRequest) -> OrganResponse:
        if request.organ_id == "planner" and self.plans:
            self.requests.append(request)
            return OrganResponse(organ_id="planner", suggestion=self.plans.pop(0))
        return await super().call(request)


class Env:
    """同一个数据库上重建心跳，搭流程测试。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = ManualClock(NOW)
        self.opened: list[Rig] = []
        self.people: list[PeopleDirectory] = []

    def build(
        self,
        judge: list[str | BaseException],
        *,
        hormones_enabled: bool = True,
        config: HormonesConfig | None = None,
        plans: list[str] | None = None,
    ) -> tuple[Rig, Hormones | None]:
        if config is None:
            config = HormonesConfig(enabled=True)
        db_path = self.tmp_path / "aureus.db"
        ledger = Ledger(db_path, self.clock, calendar=CALENDAR)
        timeline = TimelineDatabase(db_path, self.clock)
        people = PeopleDirectory(db_path, self.clock)
        for opened in (ledger, timeline, people):
            opened.open()
        people.ensure_user("阿明", [(CHANNEL, "local")])
        stop = EmergencyStop(self.tmp_path / "halt.json", self.clock)
        organs = PlanOrgans(judge, plans or [])
        hormones = Hormones(config) if hormones_enabled else None
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


async def test_retaliation_is_allowed_when_anger_is_high(env: Env) -> None:
    rig, _ = env.build([_judgment(1.0)], config=ANGRY)
    await rig.exchange("你这个破 AI")

    [planner] = [r for r in rig.organs.requests if r.organ_id == "planner"]
    [replyer] = [r for r in rig.organs.requests if r.organ_id == "replyer"]
    for request in (planner, replyer):
        assert "允许质疑、反驳" in request.system_context
        assert "不许辱骂" in request.system_context
    [reply] = [m for m in rig.records(EVENT_MESSAGE) if m.actor_type.value == "me"]
    assert reply.metadata["hormones"]["retaliation"] == "allowed"


async def test_retaliation_is_capped_by_code_not_by_the_model(env: Env) -> None:
    config = HormonesConfig(
        enabled=True, retaliate_at=0.25, cold_shoulder_at=0.9, max_retaliations=2
    )
    rig, _ = env.build([_judgment(1.0)] * 3, config=config)
    for text in ("破 AI 1", "破 AI 2", "破 AI 3"):
        await rig.exchange(text)

    replyers = [r for r in rig.organs.requests if r.organ_id == "replyer"]
    assert "不再回击" not in replyers[1].system_context
    assert "不再回击" in replyers[2].system_context
    assert "允许质疑" not in replyers[2].system_context


async def test_cold_shoulder_is_allowed_once_then_a_reply_is_forced(env: Env) -> None:
    rig, _ = env.build([_judgment(1.0)] * 2, config=ANGRY, plans=[DECLINE, DECLINE])
    await rig.exchange("骂你骂你")

    [planner] = [r for r in rig.organs.requests if r.organ_id == "planner"]
    assert "可以选择这次不回复" in planner.system_context
    [declined] = rig.records(EVENT_REPLY_DECLINED)
    assert declined.metadata["hormones"]["cold_shoulder_allowed"] is True
    assert rig.replies() == []

    # 第二次规划器还是想不回，但这场冲突里已经不回过一次，代码要求必须回。
    await rig.exchange("再骂你")
    assert len(rig.records(EVENT_REPLY_DECLINED)) == 1
    [reply] = [m for m in rig.records(EVENT_MESSAGE) if m.actor_type.value == "me"]
    assert reply.metadata["forced_reply"] is True
    assert reply.metadata["hormones"]["must_reply"] is True


async def test_without_anger_the_planner_may_still_decline_freely(env: Env) -> None:
    rig, _ = env.build(['{"others": [], "toward_me": []}'], plans=[DECLINE])
    await rig.exchange("嗯")

    assert len(rig.records(EVENT_REPLY_DECLINED)) == 1
    assert rig.replies() == []


async def test_hurt_shortens_the_reply_by_one_level(env: Env) -> None:
    config = HormonesConfig(enabled=True, hurt_shorten_at=0.3)
    plan = '{"reply": true, "intent": "回应", "length": "长", "reason": "要回"}'
    rig, _ = env.build([REJECTION], config=config, plans=[plan])
    await rig.exchange("我不想理你")

    [replyer] = [r for r in rig.organs.requests if r.organ_id == "replyer"]
    # 规划器要长回复，低落把上限降到「中」，回复器只被要求三五句话。
    assert "三五句话" in replyer.prompt + replyer.system_context
    assert "可以展开说" not in replyer.prompt + replyer.system_context
    [reply] = [m for m in rig.records(EVENT_MESSAGE) if m.actor_type.value == "me"]
    assert reply.metadata["hormones"]["shorten_reply"] is True


async def test_surprise_is_detected_from_the_hormone_level(env: Env) -> None:
    rig, hormones = env.build([])
    assert hormones is not None
    assert rig.heartbeat._surprised() is False  # pyright: ignore[reportPrivateUsage]
    hormones.add(
        [
            HormoneTrigger(
                kind=HormoneKind.SURPRISE,
                rise=0.6,
                triggered_at=env.clock.now(),
                person_id=None,
                reason="结果和预期差很多",
                source_record_id=1,
                source_visibility=VisibilityScope.PRIVATE,
            )
        ],
        env.clock.now(),
    )
    assert rig.heartbeat._surprised() is True  # pyright: ignore[reportPrivateUsage]


async def test_disabled_hormones_means_no_behaviour_changes(env: Env) -> None:
    rig, _ = env.build([_judgment(1.0)], hormones_enabled=False)
    await rig.exchange("你这个破 AI")

    [replyer] = [r for r in rig.organs.requests if r.organ_id == "replyer"]
    assert "怒气" not in replyer.system_context
    assert "允许质疑" not in replyer.system_context
    [reply] = [m for m in rig.records(EVENT_MESSAGE) if m.actor_type.value == "me"]
    assert "hormones" not in reply.metadata
