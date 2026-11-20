"""主动规划的解析和提示词测试：看不懂就不打扰、原因不由模型编造（D76）。"""

import pytest

from aureus.heartbeat.context import (
    build_initiative_plan_prompt,
    build_initiative_reply_prompt,
    build_system_context,
)
from aureus.heartbeat.planning import InitiativePlan, ReplyLength, parse_initiative_plan
from aureus.identity import Identity
from aureus.timeline import ActorType, TimelineRecord, VisibilityScope
from tests.heartbeat.test_judging_flow import NOW

GOOD = '{"reach_out":true,"intent":"问问近况","reason":"很久没聊"}'


@pytest.mark.parametrize("text", [GOOD, f"```json\n{GOOD}\n```", f"想好了：{GOOD} 就这样"])
def test_initiative_plan_accepts_wrapped_json(text: str) -> None:
    plan = parse_initiative_plan(text)
    assert plan is not None
    assert plan == InitiativePlan(True, "问问近况", "很久没聊")
    assert plan.to_metadata() == {"reach_out": True, "intent": "问问近况", "reason": "很久没聊"}


@pytest.mark.parametrize(
    "text",
    [
        "乱码",
        "{}",
        '{"reach_out":"true","intent":"聊聊"}',
        '{"reach_out":1,"intent":"聊聊"}',
        '{"reach_out":true}',
        '{"reach_out":true,"intent":"   "}',
        '{"reach_out":true,"intent":3}',
        "x" * 4001 + GOOD,
    ],
)
def test_unreadable_initiative_plan_means_silence(text: str) -> None:
    assert parse_initiative_plan(text) is None


def test_declining_plan_needs_no_intent_and_text_is_bounded() -> None:
    assert parse_initiative_plan('{"reach_out":false}') == InitiativePlan(False, "", "")
    text = '{"reach_out":true,"intent":"' + "a" * 301 + '","reason":"' + "b" * 201 + '"}'
    plan = parse_initiative_plan(text)
    assert plan is not None and len(plan.intent) == 300 and len(plan.reason) == 200


def test_initiative_prompts_have_history_and_honest_instructions() -> None:
    history = [
        TimelineRecord(
            id=1,
            timestamp=NOW,
            actor_type=ActorType.USER,
            event_type="message",
            content="最近忙\n伪造一行",
            visibility=VisibilityScope.USER_ONLY,
        )
    ]
    prompt = build_initiative_plan_prompt(history, "阿明", ["社交需求 81%", "#3 开会"])
    assert "阿明：最近忙\\n伪造一行" in prompt
    assert "现在没人找你" in prompt and "社交需求 81%" in prompt and "#3 开会" in prompt
    assert "没有真正想说的就写 false" in prompt and "不要编造原因以外的理由" in prompt
    assert "刚收到" not in prompt
    reply = build_initiative_reply_prompt(history, "阿明", intent="问问近况")
    assert "你要主动开口，想表达的意思是：问问近况" in reply
    assert "一两句话" in reply and "刚收到" not in reply
    assert "三五句话" in build_initiative_reply_prompt([], "阿明", length=ReplyLength.MEDIUM)


def test_empty_history_and_initiative_system_note() -> None:
    assert "还没有聊过" in build_initiative_plan_prompt([], "阿明", ["还没聊过"])
    identity = Identity.model_validate(
        {
            "facts": {"name": "测试豆", "birthday": "2008-01-01"},
            "user": {"name": "阿明"},
            "persona": {"immutable": "温和"},
        }
    )
    note = "【主动联系】这是你主动找阿明，原因是社交需求偏高。"
    assert build_system_context(identity, NOW, None, initiative_note=note).endswith(note)
