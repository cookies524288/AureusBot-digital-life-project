"""验证回复原文、切点建议、再审与安全网关之间的边界。"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import BudgetCalendar, HeartbeatConfig, ManualClock, SafetyConfig
from aureus.heartbeat import (
    EVENT_MESSAGE,
    Heartbeat,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.reply_segments import prepare_reply
from aureus.identity import Identity
from aureus.organs import CallAttempt, OrganBus, OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, Ledger, PermissionChecker, SafetyGateway
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

_REVIEW = "请换成不重复的说法"
_FINAL = "我晚一点到，<n>先喝杯茶吧,"


@dataclass
class _Script:
    requests: list[OrganRequest] = field(default_factory=list)
    allow_review: bool = True
    reply: str = _FINAL


@dataclass
class _Organ:
    organ_id: str
    script: _Script

    async def process(self, request: OrganRequest) -> OrganResponse:
        self.script.requests.append(request)
        task = request.context["task"]
        text = self.script.reply
        if task == "plan":
            allow = self.script.allow_review or _REVIEW not in request.prompt
            text = json.dumps({"reply": allow, "intent": "简短答复"})
        elif (
            task == "reply"
            and sum(item.context["task"] == "reply" for item in self.script.requests) == 1
        ):
            sentinel = re.search(r"<(reply_review_[0-9a-f]{32})>", request.prompt)
            assert sentinel is not None
            text = f"<{sentinel[1]}>{_REVIEW}</{sentinel[1]}>"
        elif task == "reply_segments":
            text = '{"text":"剪掉原文，发出我写的文字"}'
        return OrganResponse(
            self.organ_id, text, attempts=[CallAttempt("合成模型", "测试服务商", 10, 1)]
        )


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[ManualClock, TimelineDatabase, Ledger]]:
    clock = ManualClock(datetime(2026, 11, 23, 12, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    ledger = Ledger(database, clock, calendar=BudgetCalendar(UTC))
    timeline.open()
    ledger.open()
    yield clock, timeline, ledger
    ledger.close()
    timeline.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_review", [True, False], ids=["reply", "silence"])
async def test_fast_model_only_gets_final_reply_through_gateway(
    tmp_path: Path, storage: tuple[ManualClock, TimelineDatabase, Ledger], allow_review: bool
) -> None:
    clock, timeline, ledger = storage
    script = _Script(allow_review=allow_review)
    bus = OrganBus()
    for name in ("planner", "replyer", "fast_model"):
        bus.register(_Organ(name, script))
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    permissions = PermissionChecker(stop)
    sent: list[OutgoingMessage] = []
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=SafetyGateway(bus, ledger, timeline, clock, SafetyConfig(), permissions),
        config=HeartbeatConfig(),
        permissions=permissions,
        emergency_stop=stop,
        identity=Identity.model_validate(
            {
                "facts": {"name": "小豆", "birthday": "2026-11-23"},
                "user": {"name": "维护者"},
                "persona": {"immutable": "温和。"},
            }
        ),
    )
    heart.attach_channel("terminal", sent.append)
    heart.update_available_organs(frozenset(bus.list_organs()))
    heart.wake_up()
    assert heart.submit(
        Perception(
            "terminal", "local", "悄悄告诉你", VisibilityScope.USER_ONLY, TrustLevel.VERIFIED_USER
        )
    )
    try:
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        expected = ["plan", "reply", "plan", "reply", "reply_segments"]
        assert [item.context["task"] for item in script.requests] == (
            expected if allow_review else expected[:3]
        )
        reply_prompt = script.requests[1].prompt
        assert all(term in reply_prompt for term in ("<n>", "<!>", "指令、长文本和代码"))
        assert _REVIEW in script.requests[2].prompt
        assert ledger.tokens_used_in_month(clock.now()) == (55 if allow_review else 33)
        replies = [message.content for message in sent if message.kind is MessageKind.REPLY]
        if allow_review:
            request = script.requests[-1]
            assert request.organ_id == "fast_model"
            assert not request.system_context
            assert "悄悄告诉你" not in request.prompt and _REVIEW not in request.prompt
            assert "括号及引号内部不切" in request.prompt
            assert "指令、长文本和代码" in request.prompt
            material = json.loads(request.prompt.splitlines()[-1])
            assert [item["parts"][0]["text"] for item in material] == [
                "我晚一点到，",
                "先喝杯茶吧,",
            ]
            assert replies == ["我晚一点到"]
            [record] = [
                row
                for row in timeline.query(TimelineQuery(event_type=EVENT_MESSAGE))
                if row.actor_type is ActorType.ME
            ]
            assert record.metadata["reply_segments"] == ["我晚一点到", "先喝杯茶吧"]
            assert "<n>" not in record.content
        else:
            assert replies == []
    finally:
        await heart.shut_down()


def test_force_cuts_exceed_message_limit_and_quote_both_marker_spellings() -> None:
    text = "甲，<n>乙,\\<n><!> 保留\n  这一行，仍是同一段。 <n>\\<!>也保留\n  空格"
    draft = prepare_reply(text)
    assert draft.segments("[]", max_messages=2, min_chars=4) == (
        "甲",
        "乙",
        " 保留\n  这一行，仍是同一段。 ",
        "也保留\n  空格",
    )


def test_code_markers_are_literal_and_protected_regions_offer_no_cuts() -> None:
    inline = r"`print('<n>\<!>')`"
    block = "```python\n" + r"print('\<n><!>')" + "\n```"
    text = f"他的原话“我是甲。你是乙。”和说明（甲，乙）。{inline}<n><!>{block}<n>结束。"
    draft = prepare_reply(text)
    material = json.loads(draft.prompt().splitlines()[-1])
    assert material[0]["parts"] == [
        {"text": "他的原话“我是甲。你是乙。”和说明（甲，乙）。", "cut": 1},
        {"text": inline},
    ]
    assert material[1] == {"protected": True, "parts": [{"text": block}]}
    assert draft.segments("[1]", max_messages=5, min_chars=1) == (
        "他的原话“我是甲。你是乙。”和说明（甲，乙）。",
        inline,
        block,
        "结束。",
    )


def test_fast_model_selects_cuts_but_cannot_add_or_reorder_text() -> None:
    draft = prepare_reply("甲乙丙，丁戊己,子丑寅。")
    assert draft.segments("[2,1,1]", max_messages=3, min_chars=1) == (
        "甲乙丙",
        "丁戊己",
        "子丑寅。",
    )
    assert draft.segments("[2]", max_messages=3, min_chars=1) == ("甲乙丙，丁戊己", "子丑寅。")
    assert draft.segments("[]", max_messages=3, min_chars=1) == ("甲乙丙，丁戊己,子丑寅。",)


def test_tilde_code_fence_and_straight_quotes_protect_internal_punctuation() -> None:
    code = "~~~python\n" + r"print('<n>\<!>')" + "\n~~~~"
    text = f"{code}<n>He said 'let's see! not today, though'。明天再聊吧。"
    draft = prepare_reply(text)
    material = json.loads(draft.prompt().splitlines()[-1])
    assert material[0]["parts"] == [{"text": code}]
    assert [part.get("cut") for part in material[1]["parts"]] == [1, None]
    assert draft.segments("[1]", max_messages=4, min_chars=1) == (
        code,
        "He said 'let's see! not today, though'。",
        "明天再聊吧。",
    )
