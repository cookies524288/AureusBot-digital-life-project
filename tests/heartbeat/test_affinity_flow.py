"""好感接进心跳的测试（路线图 2.4，D71）：判断结果改好感、好感进上下文、判断失败不变。"""

from collections.abc import Iterator
from datetime import UTC
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, HeartbeatConfig, JudgeConfig, ManualClock, PeopleConfig
from aureus.heartbeat import Heartbeat
from aureus.identity import Identity
from aureus.people import AffinityBook, PeopleDirectory
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

USER_INITIAL = 80.0
PRAISE = '{"others": [], "toward_me": [{"kind": "praise", "intensity": 1.0, "confidence": 1.0}]}'
OFFENSE = '{"others": [], "toward_me": [{"kind": "offense", "intensity": 1.0, "confidence": 1.0}]}'
NOTHING = '{"others": [], "toward_me": []}'


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = ManualClock(NOW)
        self.closers: list[object] = []

    def build(
        self, judge: list[str | BaseException], *, with_affinity: bool = True
    ) -> tuple[Rig, AffinityBook | None, int]:
        db_path = self.tmp_path / "aureus.db"
        ledger = Ledger(db_path, self.clock, calendar=CALENDAR)
        timeline = TimelineDatabase(db_path, self.clock)
        people = PeopleDirectory(db_path, self.clock)
        for opened in (ledger, timeline, people):
            opened.open()
            self.closers.append(opened)
        user = people.ensure_user("阿明", [(CHANNEL, "local")])
        affinity = None
        if with_affinity:
            affinity = AffinityBook(
                db_path, self.clock, PeopleConfig(), CALENDAR, min_confidence=0.3
            )
            affinity.open()
            self.closers.append(affinity)
        stop = EmergencyStop(self.tmp_path / "halt.json", self.clock)
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
            judge=JudgeConfig(enabled=True),
            people=people,
            affinity=affinity,
        )
        rig = Rig(ledger, timeline, organs, beating)
        beating.attach_channel(CHANNEL, rig.sent.append)
        beating.wake_up()
        return rig, affinity, user.id

    def close(self) -> None:
        for item in reversed(self.closers):
            item.close()  # type: ignore[attr-defined]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    yield environment
    environment.close()


def _replyer_context(rig: Rig) -> str:
    return [r for r in rig.organs.requests if r.organ_id == "replyer"][-1].system_context


async def test_affinity_appears_in_the_reply_context(env: Env) -> None:
    rig, _, _ = env.build([NOTHING])
    await rig.exchange("早")
    assert "你对用户的好感：80.0 / 100" in _replyer_context(rig)


async def test_offense_lowers_and_praise_raises_affinity(env: Env) -> None:
    rig, affinity, user_id = env.build([OFFENSE, PRAISE])
    assert affinity is not None

    await rig.exchange("你这个破 AI")
    after_offense = affinity.current(user_id)
    assert after_offense < USER_INITIAL

    await rig.exchange("刚才是我不好，你挺厉害的")
    assert affinity.current(user_id) > after_offense

    # 每条变化都记着来自哪条判断记录
    judgments = {record.id for record in rig.records("judgment")}
    assert {change.source_record_id for change in affinity.history(user_id)} <= judgments


async def test_failed_judgment_leaves_affinity_unchanged(env: Env) -> None:
    rig, affinity, user_id = env.build(["完全不是 JSON"])
    assert affinity is not None
    await rig.exchange("你这个破 AI")
    assert affinity.history(user_id) == []
    assert len(rig.replies()) == 1


async def test_without_affinity_book_nothing_is_added(env: Env) -> None:
    rig, _, _ = env.build([OFFENSE], with_affinity=False)
    await rig.exchange("你这个破 AI")
    assert "好感" not in _replyer_context(rig)
    assert len(rig.replies()) == 1
