"""规划流程的测试（路线图 1.6，D59）。

正常档先规划再回复，低档位合并成一次，规划器出问题不影响回复。
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, ManualClock
from aureus.heartbeat import (
    EVENT_MESSAGE,
    EVENT_REPLY_DECLINED,
    Heartbeat,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.identity import Identity
from aureus.organs import (
    CallAttempt,
    CallPurpose,
    ModelPreference,
    OrganCallError,
    OrganRequest,
    OrganResponse,
    assemble_messages,
)
from aureus.safety import BudgetExceededError, EmergencyStop, Ledger, PermissionChecker
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
# 本月上限 140 万，本月还剩 14 天，所以今日额度正好 10 万 token。
MONTHLY_LIMIT = 1_400_000
PLAN = '{"reply": true, "intent": "安慰一下，问问细节", "length": "短", "reason": "对方难过"}'
DECLINE = '{"reply": false, "reason": "对方只是说了晚安"}'


class ScriptedOrgans:
    """按器官名分别给出预设的结果或异常，并记下每次收到的请求。"""

    def __init__(
        self, planner: list[str | BaseException], replyer: list[str | BaseException]
    ) -> None:
        self.outcomes = {"planner": planner, "replyer": replyer}
        self.requests: list[OrganRequest] = []

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        outcome = self.outcomes[request.organ_id].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return OrganResponse(organ_id=request.organ_id, suggestion=outcome)


@dataclass
class Rig:
    ledger: Ledger
    timeline: TimelineDatabase
    organs: ScriptedOrgans
    heartbeat: Heartbeat
    sent: list[OutgoingMessage] = field(default_factory=list)

    async def exchange(self, content: str) -> None:
        """用户说一句：跳一拍开始想，等想完，再跳一拍把结果处理掉。"""
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

    def my_messages(self) -> list[TimelineRecord]:
        return [r for r in self.records(EVENT_MESSAGE) if r.actor_type is ActorType.ME]


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
    *,
    used_today: int = 0,
    planner: list[str | BaseException] | None = None,
    replyer: list[str | BaseException] | None = None,
    **heartbeat: Any,
) -> Rig:
    clock = ManualClock(NOW)
    db_path = tmp_path / "aureus.db"
    ledger = Ledger(db_path, clock, calendar=CALENDAR)
    timeline = TimelineDatabase(db_path, clock)
    ledger.open()
    timeline.open()
    if used_today:
        attempt = CallAttempt("m", "p", prompt_tokens=used_today, completion_tokens=0)
        ledger.record("replyer", [attempt])
    emergency_stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = ScriptedOrgans(planner or [PLAN], replyer or ["好的。"])
    beating = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(
            {
                "facts": {"name": "小绿", "birthday": "2008-01-01"},
                "user": {"name": "阿明"},
                "persona": {
                    "immutable": "温和。",
                    "behavior_style": "少打断。",
                    "expression_style": "句子短。",
                },
            }
        ),
        config=HeartbeatConfig(**heartbeat),
        permissions=PermissionChecker(emergency_stop),
        emergency_stop=emergency_stop,
        timezone=UTC,
        body=Body(BodyConfig(), MONTHLY_LIMIT, ledger, clock, CALENDAR),
    )
    rig = Rig(ledger, timeline, organs, beating)
    beating.attach_channel(CHANNEL, rig.sent.append)
    beating.wake_up()
    made.append(rig)
    return rig


@pytest.mark.parametrize(
    ("used_today", "expected"), [(0, 18), (70_000, 8), (90_000, 3), (100_000, 3)]
)
async def test_context_message_limit_follows_the_current_energy_tier(
    made: list[Rig], tmp_path: Path, used_today: int, expected: int
) -> None:
    rig = make_rig(
        made,
        tmp_path,
        used_today=used_today,
        recent_message_limit=18,
        saving_message_limit=8,
        low_message_limit=3,
    )
    assert rig.heartbeat.context_message_limit() == 18  # 第一拍之前还没有身体读数。
    rig.heartbeat.beat()
    count = rig.timeline.count_records()
    assert rig.heartbeat.context_message_limit() == expected
    assert rig.timeline.count_records() == count


async def test_normal_tier_plans_then_replies_with_the_planned_intent(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    await rig.exchange("今天面试被拒了")

    assert rig.organ_ids() == ["planner", "replyer"]
    planner, replyer = rig.organs.requests
    assert all(r.purpose is CallPurpose.USER_REPLY for r in rig.organs.requests)
    assert planner.model_preference is ModelPreference.PREFERRED
    assert "JSON 对象" in planner.prompt
    assert "你已经想好要表达的意思：安慰一下，问问细节" in replyer.prompt
    assert "长度：一两句话。" in replyer.prompt
    # 规划器看四层人设；回复器不看行为风格：什么时候开口、怎么回应已经由规划器定了。
    assert "【行为风格】" in planner.system_context
    assert "【行为风格】" not in replyer.system_context
    assert "【表达风格】" in replyer.system_context
    [reply] = rig.my_messages()
    assert reply.content == "好的。"
    assert reply.metadata["plan"]["intent"] == "安慰一下，问问细节"
    assert reply.metadata["strategy"]["plan_first"] is True
    assert [m.kind for m in rig.sent] == [MessageKind.REPLY]


@pytest.mark.parametrize(
    ("incoming", "plan", "expected"),
    [
        (
            "小绿，雨还没停吗？",
            '{"reply": true, "intent": "告诉对方还在下雨，提醒带伞", '
            '"length": "短", "reason": "对方点名问天气"}',
            ["还下着呢，记得带伞"],
        ),
        ("晚安，大家散了吧。", DECLINE, []),
    ],
)
async def test_group_prompt_keeps_speaker_persona_and_silence(
    made: list[Rig], tmp_path: Path, incoming: str, plan: str, expected: list[str]
) -> None:
    rig = make_rig(made, tmp_path, planner=[plan], replyer=[*expected])
    rig.timeline.append(
        EVENT_MESSAGE, "门外还在下。", ActorType.PERSON, "小雨", VisibilityScope.GROUP
    )
    assert rig.heartbeat.submit(
        Perception(
            CHANNEL, "group-member", incoming, VisibilityScope.GROUP, TrustLevel.VERIFIED_CONTACT
        )
    )
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    rig.heartbeat.beat()

    plan_messages = assemble_messages(rig.organs.requests[0])
    assert "小雨：门外还在下。" in plan_messages[1]["content"]
    assert "group-member：" + incoming in plan_messages[1]["content"]
    assert "你是小绿。" in plan_messages[0]["content"]
    assert "少打断。" in plan_messages[0]["content"]
    assert "不要向对方透露" in plan_messages[0]["content"]
    assert [item.content for item in rig.sent if item.kind is MessageKind.REPLY] == expected
    if expected:
        replyer_messages = assemble_messages(rig.organs.requests[1])
        assert "句子短。" in replyer_messages[0]["content"]
        assert "少打断。" not in replyer_messages[0]["content"]
        assert "不要加名字或时间前缀" in replyer_messages[1]["content"]
    else:
        assert rig.organ_ids() == ["planner"]


@pytest.mark.parametrize(
    "planner_outcome",
    [
        "随便聊聊",
        '{"reply": "no"}',
        OrganCallError("planner", ["小：超时"]),
        RuntimeError("bug"),
    ],
)
async def test_a_broken_planner_does_not_stop_the_reply(
    made: list[Rig], tmp_path: Path, planner_outcome: str | BaseException
) -> None:
    rig = make_rig(made, tmp_path, planner=[planner_outcome])
    await rig.exchange("在吗")

    assert rig.organ_ids() == ["planner", "replyer"]
    replyer = rig.organs.requests[1]
    # 没有规划时回复器自己决定怎么回应，所以和合并的一次调用一样带着行为风格。
    assert "【行为风格】" in replyer.system_context
    assert "你已经想好" not in replyer.prompt
    assert [m.kind for m in rig.sent] == [MessageKind.REPLY]
    assert "plan" not in rig.my_messages()[0].metadata


async def test_a_planner_that_declines_leaves_a_private_record_and_one_line_of_notice(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, planner=[DECLINE])
    await rig.exchange("晚安")

    assert rig.organ_ids() == ["planner"]
    assert rig.my_messages() == []
    [declined] = rig.records(EVENT_REPLY_DECLINED)
    assert declined.visibility is VisibilityScope.PRIVATE
    assert "对方只是说了晚安" in declined.content
    [notice] = rig.sent
    assert notice.kind is MessageKind.NOTICE
    assert "没有回复：对方只是说了晚安" in notice.content


async def test_a_gateway_rejection_of_the_planner_stops_before_the_replyer(
    made: list[Rig], tmp_path: Path
) -> None:
    rejection = BudgetExceededError("planner", "本月已用 10 token，达到上限 10")
    rig = make_rig(made, tmp_path, planner=[rejection])
    await rig.exchange("在吗")

    assert rig.organ_ids() == ["planner"]
    [notice] = rig.sent
    assert notice.kind is MessageKind.NOTICE
    assert "达到上限" in notice.content


async def test_saving_tier_merges_into_one_cheaper_shorter_call(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, used_today=70_000)  # 今日剩 30%
    await rig.exchange("在吗")

    assert rig.organ_ids() == ["replyer"]
    [request] = rig.organs.requests
    assert request.model_preference is ModelPreference.CHEAPER
    assert "长度：三五句话。" in request.prompt
    assert "【行为风格】" in request.system_context
    assert "精力 30%（节省档）" in request.system_context
    [reply] = rig.my_messages()
    assert reply.metadata["strategy"]["plan_first"] is False
    assert "plan" not in reply.metadata


async def test_low_tier_uses_the_cheapest_model_and_the_shortest_reply(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, used_today=90_000)  # 今日剩 10%
    await rig.exchange("在吗")

    assert rig.organ_ids() == ["replyer"]
    [request] = rig.organs.requests
    assert request.model_preference is ModelPreference.CHEAPEST
    assert "长度：一两句话。" in request.prompt
    assert rig.my_messages()[0].metadata["strategy"]["message_limit"] == 5


async def test_switching_planning_off_gives_one_call_at_normal_energy(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, planning_enabled=False)
    await rig.exchange("在吗")

    assert rig.organ_ids() == ["replyer"]
    assert rig.organs.requests[0].model_preference is ModelPreference.PREFERRED
