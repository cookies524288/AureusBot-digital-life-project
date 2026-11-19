"""判断器官接进心跳的测试（路线图 2.2，D68）。

判断在规划之前，任何失败都不影响回复；低档位和开关关着时不调用；每次调用留一条记录。
"""

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, JudgeConfig, ManualClock
from aureus.heartbeat import (
    EVENT_JUDGMENT,
    EVENT_MESSAGE,
    Heartbeat,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.identity import Identity
from aureus.organs import CallAttempt, CallPurpose, OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError, EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
CALENDAR = BudgetCalendar(UTC)
CHANNEL = "terminal"
MONTHLY_LIMIT = 1_400_000  # 今日额度 10 万 token
PLAN = '{"reply": true, "intent": "回应一下", "length": "短", "reason": "要回"}'
GOOD_JUDGMENT = (
    '{"others": [{"subject": "sender", "emotion": "hurt", "intensity": 0.6, "confidence": 0.8}],'
    ' "toward_me": [{"kind": "offense", "intensity": 0.7, "confidence": 0.9}]}'
)


class ScriptedOrgans:
    """判断器官按预设给结果或异常；没配判断器官时像真网关一样报“未注册”。"""

    def __init__(self, judge: list[str | BaseException] | None) -> None:
        self.judge = judge
        self.requests: list[OrganRequest] = []

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if request.organ_id == "judge":
            if self.judge is None:
                raise ValueError("器官 judge 未注册")
            outcome = self.judge.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            await asyncio.sleep(0)
            return OrganResponse(organ_id="judge", suggestion=outcome)
        text = PLAN if request.organ_id == "planner" else "好的。"
        return OrganResponse(organ_id=request.organ_id, suggestion=text)


class SlowJudge(ScriptedOrgans):
    async def call(self, request: OrganRequest) -> OrganResponse:
        if request.organ_id == "judge":
            self.requests.append(request)
            await asyncio.sleep(5)
        return await super().call(request)


@dataclass
class Rig:
    ledger: Ledger
    timeline: TimelineDatabase
    organs: ScriptedOrgans
    heartbeat: Heartbeat
    sent: list[OutgoingMessage] = field(default_factory=list)

    async def exchange(self, content: str) -> None:
        assert self.heartbeat.submit(
            Perception(
                channel=CHANNEL,
                sender_id="local",
                content=content,
                visibility=VisibilityScope.USER_ONLY,
                trust=TrustLevel.VERIFIED_USER,
            )
        )
        self.heartbeat.beat()
        await self.heartbeat.wait_for_thought()
        self.heartbeat.beat()

    def records(self, event_type: str) -> list[TimelineRecord]:
        return list(self.timeline.query(TimelineQuery(event_type=event_type, limit=100)))

    def organ_ids(self) -> list[str]:
        return [request.organ_id for request in self.organs.requests]

    def replies(self) -> list[str]:
        return [r.content for r in self.records(EVENT_MESSAGE) if r.actor_type is ActorType.ME]


@pytest.fixture
def made() -> Iterator[list[Rig]]:
    rigs: list[Rig] = []
    yield rigs
    for rig in rigs:
        rig.ledger.close()
        rig.timeline.close()


def make_rig(
    made: list[Rig],
    tmp_path: Path,
    judge: list[str | BaseException] | None,
    *,
    enabled: bool = True,
    used_today: int = 0,
    slow: bool = False,
    timeout_seconds: float = 10.0,
    context_messages: int = 5,
) -> Rig:
    clock = ManualClock(NOW)
    db_path = tmp_path / "aureus.db"
    ledger = Ledger(db_path, clock, calendar=CALENDAR)
    timeline = TimelineDatabase(db_path, clock)
    ledger.open()
    timeline.open()
    if used_today:
        ledger.record(
            "replyer", [CallAttempt("m", "p", prompt_tokens=used_today, completion_tokens=0)]
        )
    emergency_stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = SlowJudge(judge) if slow else ScriptedOrgans(judge)
    beating = Heartbeat(
        clock=clock,
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
        permissions=PermissionChecker(emergency_stop),
        emergency_stop=emergency_stop,
        timezone=UTC,
        body=Body(BodyConfig(), MONTHLY_LIMIT, ledger, clock, CALENDAR),
        judge=JudgeConfig(
            enabled=enabled, timeout_seconds=timeout_seconds, context_messages=context_messages
        ),
    )
    rig = Rig(ledger, timeline, organs, beating)
    beating.attach_channel(CHANNEL, rig.sent.append)
    beating.wake_up()
    made.append(rig)
    return rig


async def test_judges_before_planning_and_records_the_result(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, [GOOD_JUDGMENT])
    await rig.exchange("你这个破 AI")

    assert rig.organ_ids() == ["judge", "planner", "replyer"]
    judge = rig.organs.requests[0]
    assert judge.purpose is CallPurpose.OPTIONAL
    # 只看不变人格和可变人格，不看行为风格。
    assert "【不变人格】" in judge.system_context
    assert "【行为风格】" not in judge.system_context
    [record] = rig.records(EVENT_JUDGMENT)
    assert record.visibility is VisibilityScope.PRIVATE
    assert record.metadata["judgment"]["toward_me"][0]["kind"] == "offense"
    assert len(record.metadata["in_reply_to"]) == 1


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("乱码", "看不懂"),
        ('{"others": [], "toward_me": [{"kind": "x", "intensity": 1, "confidence": 1}]}', "看不懂"),
        (OrganCallError("judge", ["小：超时"]), "调用失败"),
        (RuntimeError("bug"), "调用出错"),
        (CallRejectedError("judge", "可选额度用完"), "可选额度用完"),
    ],
)
async def test_a_failed_judgment_leaves_the_reply_unchanged(
    made: list[Rig], tmp_path: Path, outcome: str | BaseException, reason: str
) -> None:
    rig = make_rig(made, tmp_path, [outcome])
    await rig.exchange("在吗")

    assert rig.organ_ids() == ["judge", "planner", "replyer"]
    assert rig.replies() == ["好的。"]
    [record] = rig.records(EVENT_JUDGMENT)
    assert "judgment" not in record.metadata
    assert reason in record.metadata["failure"]


async def test_a_judge_that_times_out_is_skipped(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, [GOOD_JUDGMENT], slow=True, timeout_seconds=0.05)
    await rig.exchange("在吗")

    assert rig.replies() == ["好的。"]
    [record] = rig.records(EVENT_JUDGMENT)
    assert record.metadata["failure"] == "超时"


async def test_disabled_judge_is_never_called(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, [GOOD_JUDGMENT], enabled=False)
    await rig.exchange("在吗")

    assert "judge" not in rig.organ_ids()
    assert rig.records(EVENT_JUDGMENT) == []


async def test_low_energy_skips_the_judge(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path, [GOOD_JUDGMENT], used_today=90_000)  # 今日剩 10%
    await rig.exchange("在吗")

    assert "judge" not in rig.organ_ids()
    assert rig.replies() == ["好的。"]
    assert rig.records(EVENT_JUDGMENT) == []


async def test_missing_judge_organ_is_skipped_and_logged_once(
    made: list[Rig], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(made, tmp_path, None)
    with caplog.at_level(logging.WARNING):
        await rig.exchange("在吗")
        await rig.exchange("还在吗")

    assert rig.replies() == ["好的。", "好的。"]
    assert rig.records(EVENT_JUDGMENT) == []
    assert sum("没有配判断器官" in r.getMessage() for r in caplog.records) == 1


async def test_judge_gets_only_the_configured_amount_of_history(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, [GOOD_JUDGMENT, GOOD_JUDGMENT], context_messages=0)
    await rig.exchange("第一句")
    await rig.exchange("第二句")

    prompts = [r.prompt for r in rig.organs.requests if r.organ_id == "judge"]
    assert "第一句" in prompts[0]
    assert "第一句" not in prompts[1]
    assert "第二句" in prompts[1]
