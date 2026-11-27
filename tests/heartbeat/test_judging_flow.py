"""判断器官接进心跳的测试（路线图 2.2，D68；D110 改为与规划并行）。

判断和规划从同一轮材料同时出发，任何失败都不影响回复；低档位和开关关着时不调用；
结果由心跳在后续拍收取，每次真正调用留一条记录。
"""

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, JudgeConfig, ManualClock
from aureus.heartbeat import (
    EVENT_JUDGMENT,
    EVENT_MESSAGE,
    Heartbeat,
    MessageRoute,
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
        from tests.planner_fixtures import plan_response

        if request.context.get("task") == "plan":
            return plan_response(request, text)
        return OrganResponse(organ_id=request.organ_id, suggestion=text)


class SlowJudge(ScriptedOrgans):
    async def call(self, request: OrganRequest) -> OrganResponse:
        if request.organ_id == "judge":
            self.requests.append(request)
            await asyncio.sleep(5)
        return await super().call(request)


class GatedJudge(ScriptedOrgans):
    """第一次判断卡在闸门上，之后按剧本返回；被取消时记下来，便于断言。"""

    def __init__(
        self,
        judgments: list[str | BaseException],
        gate: asyncio.Event,
        started: asyncio.Event,
    ) -> None:
        super().__init__(judgments)
        self.gate = gate
        self.started = started
        self.cancelled = False
        self.block_next = True

    async def call(self, request: OrganRequest) -> OrganResponse:
        if request.organ_id != "judge":
            return await super().call(request)
        self.requests.append(request)
        assert self.judge is not None
        outcome = self.judge.pop(0)
        if self.block_next:
            self.block_next = False
            self.started.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(outcome, BaseException):
            raise outcome
        return OrganResponse(organ_id="judge", suggestion=outcome)


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
        await self.heartbeat.beat()
        await self.heartbeat.wait_for_thought()
        await self.heartbeat.beat()
        await self.settle_judgments()

    async def settle_judgments(self, timeout: float = 6.0) -> None:
        """等后台判断跑完，再跳一拍让心跳收取（D110：判断不再挡着回复）。"""
        pending = self.heartbeat._pending_judgments
        if pending:
            await asyncio.wait({item.task for item in pending}, timeout=timeout)
        await self.heartbeat.beat()

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
    organs: ScriptedOrgans | None = None,
    config: HeartbeatConfig | None = None,
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
    organs = organs or (SlowJudge(judge) if slow else ScriptedOrgans(judge))
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
        config=config or HeartbeatConfig(),
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


async def test_judgment_runs_alongside_planning_and_records_the_result(
    made: list[Rig], tmp_path: Path
) -> None:
    """D110：判断和规划并行，不再规定谁先调用；判断结果仍按时记进时间线。"""
    rig = make_rig(made, tmp_path, [GOOD_JUDGMENT])
    await rig.exchange("你这个破 AI")

    assert set(rig.organ_ids()) == {"judge", "planner", "replyer"}
    [judge] = [request for request in rig.organs.requests if request.organ_id == "judge"]
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

    assert set(rig.organ_ids()) == {"judge", "planner", "replyer"}
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


async def test_late_judgment_keeps_the_round_key_and_does_not_extend_typing(
    made: list[Rig], tmp_path: Path
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    organs = GatedJudge([GOOD_JUDGMENT], gate, started)
    rig = make_rig(made, tmp_path, [], organs=organs)
    assert rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local",
            content="你这家伙",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    await rig.heartbeat.beat()
    await asyncio.wait_for(started.wait(), timeout=5)
    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()

    records = rig.records(EVENT_MESSAGE)
    user = next(record for record in records if record.actor_type is ActorType.USER)
    reply = next(record for record in records if record.actor_type is ActorType.ME)
    # 正文已经投递、想法也结束了；判断还在跑，不能再让输入提示一直亮着。
    assert rig.heartbeat.is_thinking is False
    assert not rig.heartbeat.is_replying_to(web_visibility=VisibilityScope.USER_ONLY)

    gate.set()
    await rig.settle_judgments()
    [judgment] = rig.records(EVENT_JUDGMENT)
    assert judgment.metadata["round_id"] == reply.metadata["round_id"]
    assert judgment.metadata["in_reply_to"] == [user.id]


async def test_declined_reply_still_collects_the_judgment(made: list[Rig], tmp_path: Path) -> None:
    class DecliningOrgans(ScriptedOrgans):
        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.context.get("task") != "plan":
                return await super().call(request)
            self.requests.append(request)
            from tests.planner_fixtures import plan_response

            return plan_response(request, {"reply": False, "reason": "不想回"})

    rig = make_rig(made, tmp_path, [], organs=DecliningOrgans([GOOD_JUDGMENT]))
    await rig.exchange("在吗")

    assert rig.replies() == []
    assert len(rig.records(EVENT_JUDGMENT)) == 1


async def test_reply_failure_still_collects_the_judgment(made: list[Rig], tmp_path: Path) -> None:
    class FailingReplyer(ScriptedOrgans):
        async def call(self, request: OrganRequest) -> OrganResponse:
            if request.organ_id != "replyer":
                return await super().call(request)
            self.requests.append(request)
            raise OrganCallError("replyer", ["模型失败"])

    rig = make_rig(made, tmp_path, [], organs=FailingReplyer([GOOD_JUDGMENT]))
    await rig.exchange("在吗")

    assert rig.replies() == []
    assert len(rig.records(EVENT_JUDGMENT)) == 1


async def test_emergency_stop_cancels_pending_judgment_without_state(
    made: list[Rig], tmp_path: Path
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    organs = GatedJudge([GOOD_JUDGMENT], gate, started)
    rig = make_rig(made, tmp_path, [], organs=organs)
    assert rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local",
            content="在吗",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    await rig.heartbeat.beat()
    await asyncio.wait_for(started.wait(), timeout=5)
    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()

    rig.heartbeat.halt("测试急停")
    for _ in range(50):
        await rig.heartbeat.beat()
        if not rig.heartbeat._pending_judgments:
            break
        await asyncio.sleep(0)

    assert organs.cancelled
    assert rig.heartbeat._pending_judgments == []
    assert rig.records(EVENT_JUDGMENT) == []


async def test_shutdown_cancels_pending_judgment_without_deadlock(
    made: list[Rig], tmp_path: Path
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    organs = GatedJudge([GOOD_JUDGMENT], gate, started)
    rig = make_rig(
        made,
        tmp_path,
        [],
        organs=organs,
        config=HeartbeatConfig(shutdown_grace_seconds=0.01),
    )
    assert rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local",
            content="在吗",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    await rig.heartbeat.beat()
    await asyncio.wait_for(started.wait(), timeout=5)
    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()

    await asyncio.wait_for(rig.heartbeat.shut_down(), timeout=5)

    assert organs.cancelled
    assert rig.heartbeat._pending_judgments == []
    assert rig.records(EVENT_JUDGMENT) == []


async def test_lost_source_permission_discards_late_judgment(
    made: list[Rig], tmp_path: Path
) -> None:
    class FakeInbox:
        allowed = True

        def allows(self, key: object) -> bool:
            return self.allowed

        def permits(self, residence: object) -> bool:
            return False

    gate, started = asyncio.Event(), asyncio.Event()
    organs = GatedJudge([GOOD_JUDGMENT], gate, started)
    rig = make_rig(made, tmp_path, [], organs=organs)
    assert rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local",
            content="在吗",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
            route=MessageRoute("bot", "group", "group-1", "message-1"),
        )
    )
    await rig.heartbeat.beat()
    await asyncio.wait_for(started.wait(), timeout=5)
    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()

    [pending] = rig.heartbeat._pending_judgments
    inbox = FakeInbox()
    inbox.allowed = False
    rig.heartbeat._platform_inbox = cast(Any, inbox)
    gate.set()
    await asyncio.wait({pending.task}, timeout=5)
    await rig.heartbeat._collect_finished_judgments()

    assert organs.cancelled is False  # 任务本身跑完了，是采纳前复核时发现来源已撤权
    assert rig.heartbeat._pending_judgments == []
    assert rig.records(EVENT_JUDGMENT) == []


async def test_invalidated_round_discards_late_judgment(made: list[Rig], tmp_path: Path) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    organs = GatedJudge([GOOD_JUDGMENT], gate, started)
    rig = make_rig(made, tmp_path, [], organs=organs)
    assert rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local",
            content="在吗",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    await rig.heartbeat.beat()
    await asyncio.wait_for(started.wait(), timeout=5)
    await rig.heartbeat.wait_for_thought()
    await rig.heartbeat.beat()

    [pending] = rig.heartbeat._pending_judgments
    # 防抖重开、抢占和撤权都走这条路径：轮次作废后，迟到的判断不再写状态。
    rig.heartbeat._invalidate_judgments(round_trace=pending.round_trace)
    await asyncio.wait({pending.task}, timeout=5)
    await rig.heartbeat._collect_finished_judgments()

    assert organs.cancelled
    assert rig.heartbeat._pending_judgments == []
    assert rig.records(EVENT_JUDGMENT) == []
