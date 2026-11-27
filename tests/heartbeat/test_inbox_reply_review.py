"""检验 QQ 防抖、会话摘要和回复再审的整条心跳流程。"""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.attention import WorkingMemory
from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import (
    EVENT_MESSAGE,
    EVENT_REPLY_REFERENCE,
    Heartbeat,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.inbox_store import InboxStore
from aureus.heartbeat.perception import MessageRoute
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope
from tests.prompt_assertions import dialogue_text

_REVIEW_REASON = "这个说法像通知，改成一句口语"
_FINAL_REPLY = "行呀，周六见"


class _StreamingOrgans:
    def __init__(self, *, speak: bool) -> None:
        self.speak = speak
        self.plans: list[OrganRequest] = []
        self.replies: list[OrganRequest] = []
        self.started: asyncio.Queue[int] = asyncio.Queue()
        self.cancelled: asyncio.Queue[int] = asyncio.Queue()
        self.old_result = asyncio.Event()
        self.current_result = asyncio.Event()
        self.review_result = asyncio.Event()

    async def call(self, request: OrganRequest) -> OrganResponse:
        task = request.context.get("task")
        if task == "working_memory_summary":
            assert request.organ_id == "utils"
            source = json.loads(request.prompt)["经历"][0]["content"]
            return OrganResponse(request.organ_id, f"滚动小结：{source}")
        if task == "plan":
            assert request.stream
            self.plans.append(request)
            turn = len(self.plans)
            self.started.put_nowait(turn)
            try:
                if turn == 1:
                    await asyncio.Future[None]()
                elif turn == 2:
                    await self.current_result.wait()
                elif turn == 3:
                    await self.review_result.wait()
            except asyncio.CancelledError:
                self.cancelled.put_nowait(turn)
                if turn != 1:
                    raise
                await self.old_result.wait()
            plan = {
                1: '{"reply":true,"intent":"过期规划绝不能交给回复器"}',
                2: '{"reply":true,"intent":"用系统通知口吻确认"}',
                3: '{"reply":true,"intent":"简短答应周六见"}' if self.speak else '{"reply":false}',
            }[turn]
            from tests.planner_fixtures import plan_response

            choice = json.loads(plan)
            if choice["reply"]:
                choice["target_record_id"] = request.context["in_reply_to"][-1]
            return plan_response(request, choice)
        assert task == "reply" and not request.stream
        self.replies.append(request)
        marker = re.search(r"<(reply_review_[0-9a-f]{32})>", request.prompt)
        assert marker is not None
        sentinel = marker.group(1)
        text = (
            f"<{sentinel}>{_REVIEW_REASON}\\</{sentinel}>"
            if "用系统通知口吻确认" in dialogue_text(request)
            else _FINAL_REPLY
        )
        return OrganResponse(request.organ_id, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("speak", [True, False], ids=["rewrite", "silence"])
async def test_interrupted_group_plan_keeps_review_and_summary_in_its_conversation(
    tmp_path: Path, speak: bool
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = _StreamingOrgans(speak=speak)
    config = HeartbeatConfig(
        planner_steering_max_interrupts=1,
        split_reply_enabled=False,
        working_memory_capacity=4,
        working_memory_retain=1,
    )
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        config=config,
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
        working_memory=WorkingMemory(timeline, config),
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {"name": "维护者", "accounts": [{"platform": "qq", "id": "owner"}]},
                "persona": {"immutable": "温和。"},
            }
        ),
    )
    sent: list[OutgoingMessage] = []
    heart.attach_channel("qq", sent.append)
    heart.update_available_organs(frozenset({"utils", "planner", "replyer"}))
    heart.wake_up()

    def receive(number: str, sender: str, name: str, text: str) -> None:
        saved = store.add(
            Perception(
                "qq",
                sender,
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", "home-group", number),
            ),
            sender_name=name,
        )
        assert saved is not None
        heart.on_inbox_message(saved)
        heart.beat()

    async def start_plan(number: int) -> None:
        # steady：安静开批 4 秒、活跃开批 7 秒；多拨一点，保证已过截止时间（D142）。
        clock.advance(timedelta(seconds=7))
        heart.beat()
        assert await asyncio.wait_for(organs.started.get(), timeout=2) == number

    async def finish_thought() -> None:
        await asyncio.wait_for(heart.wait_for_thought(), timeout=2)
        heart.beat()

    try:
        for group, text in (("home-group", "约过周末碰面"), ("other-group", "别群的私事")):
            timeline.append(
                EVENT_MESSAGE,
                text,
                ActorType.PERSON,
                visibility=VisibilityScope.GROUP,
                metadata={
                    "channel": "qq",
                    "account_id": "bot",
                    "message_type": "group",
                    "recipient_id": group,
                },
            )
        timeline.append(
            EVENT_MESSAGE, "维护者的私事", ActorType.USER, visibility=VisibilityScope.USER_ONLY
        )
        timeline.append(
            EVENT_MESSAGE, "尚未压缩的私事", ActorType.USER, visibility=VisibilityScope.USER_ONLY
        )
        heart.beat()
        await finish_thought()
        assert "滚动小结：约过周末碰面" in heart.working_memory_summaries().values()

        receive("101", "owner", "阿明", "周末还是老地方吗")
        await start_plan(1)
        receive("102", "visitor", "小雨", "我说的是周六下午")
        assert await asyncio.wait_for(organs.cancelled.get(), timeout=2) == 1
        organs.old_result.set()
        await finish_thought()
        assert sent == organs.replies == []

        await start_plan(2)
        receive("103", "visitor", "小雨", "已经用完一次打断")
        organs.current_result.set()
        assert await asyncio.wait_for(organs.started.get(), timeout=2) == 3
        receive("104", "visitor", "小雨", "再审中的后来消息")
        organs.review_result.set()
        await finish_thought()

        assert len(organs.plans) == 3 and organs.cancelled.empty()
        assert len(organs.replies) == (2 if speak else 1)
        first, retried, review = organs.plans
        assert "小雨" not in first.prompt
        assert len(first.context["in_reply_to"]) == 1
        assert len(retried.context["in_reply_to"]) == 2
        assert review.context["in_reply_to"] == retried.context["in_reply_to"]
        assert all(
            request.context["in_reply_to"] == [retried.context["in_reply_to"][-1]]
            for request in organs.replies
        )
        for request in (retried, review, *organs.replies):
            text = dialogue_text(request)
            assert "滚动小结：约过周末碰面" in text
            assert "发言者 阿明（账号 owner）" in text
            assert "发言者 小雨（账号 visitor）" in text
            assert "我说的是周六下午" in text
            assert "已经用完一次打断" not in text
            assert "再审中的后来消息" not in text
            for private in ("别群的私事", "维护者的私事", "尚未压缩的私事"):
                assert private not in text + request.system_context
            assert ("请重新判断本批应回应哪句。" in text) is (request is review)
            assert _REVIEW_REASON not in text
        assert _REVIEW_REASON not in first.prompt

        [reference] = timeline.query(TimelineQuery(event_type=EVENT_REPLY_REFERENCE))
        assert reference.visibility is VisibilityScope.GROUP
        assert reference.metadata["recipient_id"] == "home-group"
        assert reference.metadata["in_reply_to"] == review.context["in_reply_to"]
        assert reference.content == ("建议回复：简短答应周六见" if speak else "建议不回复")
        assert _REVIEW_REASON not in reference.content

        recorded = [
            row.content
            for row in timeline.query(TimelineQuery(event_type=EVENT_MESSAGE))
            if row.actor_type is ActorType.ME
        ]
        if speak:
            [reply] = sent
            assert reply.content == _FINAL_REPLY
            assert reply.route == MessageRoute("bot", "group", "home-group", "102")
            assert recorded == [_FINAL_REPLY]
        else:
            assert sent == recorded == []
    finally:
        organs.old_result.set()
        organs.current_result.set()
        organs.review_result.set()
        await heart.shut_down()
        timeline.close()
