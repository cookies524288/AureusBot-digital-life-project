"""日程接进心跳的测试（路线图 2.7 验收，D74）。

规划器答应了事 → 记下、留痕、写反馈预期；到期 → 提醒一次、下次上下文写明；
做完 → 成功；做不了且有原因 → 失败；过了宽限 → 过期失败；不回复就不记；开关关掉不记。
"""

import json
from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from aureus.agenda import AgendaStore, ItemStatus
from aureus.body import Body
from aureus.common import (
    AgendaConfig,
    BodyConfig,
    FeedbackConfig,
    HeartbeatConfig,
    ManualClock,
)
from aureus.feedback import COMMITMENT_KEPT, FeedbackStore, Outcome
from aureus.heartbeat import Heartbeat
from aureus.identity import Identity
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import TimelineDatabase
from tests.heartbeat.test_hormones_behavior import PlanOrgans
from tests.heartbeat.test_judging_flow import CALENDAR, CHANNEL, MONTHLY_LIMIT, NOW, Rig

pytestmark = pytest.mark.asyncio

# NOW 是 UTC 12:00；时区用 UTC，所以「当地时间」就是 UTC。
DUE_TEXT = "2026-11-17 14:00"


def _plan(**extra: object) -> str:
    data: dict[str, object] = {"reply": True, "intent": "回应", "length": "短", "reason": "要回"}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


COMMIT = _plan(commit={"what": "下午两点提醒他开会", "due": DUE_TEXT})
DECLINE_WITH_COMMIT = json.dumps(
    {"reply": False, "intent": "", "reason": "不用回", "commit": {"what": "不该记", "due": None}},
    ensure_ascii=False,
)


# 测试里打开、最后要关上的几种存储。
Store = Ledger | TimelineDatabase | PeopleDirectory | FeedbackStore | AgendaStore


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = ManualClock(NOW)
        self.closers: list[Store] = []

    def build(
        self, plans: list[str], *, agenda_enabled: bool = True
    ) -> tuple[Rig, AgendaStore | None, FeedbackStore]:
        db_path = self.tmp_path / "aureus.db"
        ledger = Ledger(db_path, self.clock, calendar=CALENDAR)
        timeline = TimelineDatabase(db_path, self.clock)
        people = PeopleDirectory(db_path, self.clock)
        feedback = FeedbackStore(db_path, FeedbackConfig(), self.clock)
        opened: list[Store] = [ledger, timeline, people, feedback]
        agenda = None
        if agenda_enabled:
            agenda = AgendaStore(db_path, self.clock, AgendaConfig())
            opened.append(agenda)
        for item in opened:
            item.open()
            self.closers.append(item)
        people.ensure_user("阿明", [(CHANNEL, "local")])
        stop = EmergencyStop(self.tmp_path / "halt.json", self.clock)
        organs = PlanOrgans(None, plans)  # 没配判断器官：不判断
        self.organs = organs
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
            people=people,
            feedback=feedback,
            agenda=agenda,
        )
        rig = Rig(ledger, timeline, organs, beating)
        beating.attach_channel(CHANNEL, rig.sent.append)
        beating.wake_up()
        return rig, agenda, feedback

    def close(self) -> None:
        for item in reversed(self.closers):
            item.close()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    yield environment
    environment.close()


def _planner_prompts(rig: Rig) -> list[str]:
    return [r.prompt for r in rig.organs.requests if r.organ_id == "planner"]


def _reply_tool_properties(rig: Rig) -> set[str]:
    """规划器 reply 工具声明的参数名；commit 只在日程开启时才出现。"""
    request = [r for r in rig.organs.requests if r.organ_id == "planner"][0]
    [reply] = [tool for tool in request.tools if tool.name == "reply"]
    return set(reply.parameters["properties"])


def _replyer_context(rig: Rig) -> str:
    return [r for r in rig.organs.requests if r.organ_id == "replyer"][-1].system_context


async def test_a_promise_is_recorded_with_feedback_expectation(env: Env) -> None:
    rig, agenda, feedback = env.build([COMMIT])
    assert agenda is not None
    await rig.exchange("下午两点有个会，到时候提醒我")

    assert "commit" in _reply_tool_properties(rig)  # 规划器知道可以提出答应的事
    [item] = agenda.open_items()
    assert item.content == "下午两点提醒他开会"
    assert item.due_at == NOW + timedelta(hours=2)
    [added] = rig.records("agenda_added")
    assert added.metadata["item_id"] == item.id
    assert item.feedback_id is not None
    expectation = feedback.get(item.feedback_id)
    assert expectation is not None and expectation.category == COMMITMENT_KEPT
    assert expectation.expected == 0.5  # 还没有历史，用先验
    assert item.source_record_id == rig.records("message")[-1].id  # 来源是它说出口的那句


async def test_due_item_is_noticed_once_and_shows_up_next_time(env: Env) -> None:
    rig, agenda, _ = env.build([COMMIT])
    assert agenda is not None
    await rig.exchange("到时候提醒我")
    env.clock.advance(timedelta(hours=3))
    rig.heartbeat.beat()
    rig.heartbeat.beat()
    [due] = rig.records("agenda_due")
    assert "到期" in due.content
    assert "已到期 1 条" in rig.heartbeat.describe_state()

    await rig.exchange("在吗")
    context = _replyer_context(rig)
    assert "【日程】" in context and "已经到期" in context


async def test_done_counts_as_success(env: Env) -> None:
    rig, agenda, feedback = env.build([COMMIT])
    assert agenda is not None
    await rig.exchange("到时候提醒我")
    [item] = agenda.open_items()
    env.organs.plans.append(_plan(resolve=[{"id": item.id, "result": "done"}]))
    await rig.exchange("好的我去开会了")

    finished = agenda.get(item.id)
    assert finished is not None and finished.status is ItemStatus.DONE
    assert item.feedback_id is not None
    record = feedback.get(item.feedback_id)
    assert record is not None and record.outcome is Outcome.SUCCESS
    assert rig.records("agenda_closed")


async def test_dropped_with_reason_counts_as_failure(env: Env) -> None:
    rig, agenda, feedback = env.build([COMMIT])
    assert agenda is not None
    await rig.exchange("到时候提醒我")
    [item] = agenda.open_items()
    resolve = [{"id": item.id, "result": "dropped", "reason": "那时候我在关机维护"}]
    env.organs.plans.append(_plan(resolve=resolve))
    await rig.exchange("你提醒了吗")

    finished = agenda.get(item.id)
    assert finished is not None and finished.status is ItemStatus.DROPPED
    assert finished.reason == "那时候我在关机维护"
    assert item.feedback_id is not None
    record = feedback.get(item.feedback_id)
    assert record is not None and record.outcome is Outcome.FAILURE


async def test_unanswered_promise_expires_as_a_failure(env: Env) -> None:
    rig, agenda, feedback = env.build([COMMIT])
    assert agenda is not None
    await rig.exchange("到时候提醒我")
    [item] = agenda.open_items()
    env.clock.advance(timedelta(days=4))
    rig.heartbeat.beat()

    finished = agenda.get(item.id)
    assert finished is not None and finished.status is ItemStatus.EXPIRED
    assert item.feedback_id is not None
    record = feedback.get(item.feedback_id)
    assert record is not None and record.outcome is Outcome.FAILURE


async def test_resolutions_for_someone_else_are_ignored(env: Env) -> None:
    rig, agenda, _ = env.build([COMMIT])
    assert agenda is not None
    other = agenda.add(99, "别人的事", due_at=None, source_record_id=None, feedback_id=None)
    assert other is not None
    env.organs.plans.append(_plan(resolve=[{"id": other.id, "result": "done"}]))
    await rig.exchange("到时候提醒我")
    await rig.exchange("别人那件也做完了吧")
    still = agenda.get(other.id)
    assert still is not None and still.status is ItemStatus.OPEN


async def test_declined_reply_records_nothing(env: Env) -> None:
    rig, agenda, _ = env.build([DECLINE_WITH_COMMIT])
    assert agenda is not None
    await rig.exchange("嗯嗯")
    assert agenda.open_items() == []
    assert rig.records("agenda_added") == []


async def test_bad_due_time_keeps_the_item_without_a_time(env: Env) -> None:
    rig, agenda, _ = env.build([_plan(commit={"what": "过几天问问他面试结果", "due": "过几天"})])
    assert agenda is not None
    await rig.exchange("下周面试")
    [item] = agenda.open_items()
    assert item.due_at is None


async def test_agenda_switched_off_records_and_shows_nothing(env: Env) -> None:
    # 关掉日程后工具参数里没有 commit，参数模型禁止多余字段，所以这里用不带 commit 的计划。
    rig, agenda, _ = env.build([_plan()], agenda_enabled=False)
    assert agenda is None
    await rig.exchange("到时候提醒我")
    assert "commit" not in _reply_tool_properties(rig)
    assert rig.records("agenda_added") == []
    assert "【日程】" not in _replyer_context(rig)
