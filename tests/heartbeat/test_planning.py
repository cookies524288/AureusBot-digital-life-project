"""规划器建议的解析测试（路线图 1.6，D59）：读不懂的一律返回 None，由心跳照常回复。"""

import json

import pytest

from aureus.heartbeat.planning import Plan, ReplyLength, parse_plan

FULL = '{"reply": true, "intent": "安慰一下", "length": "短", "reason": "对方难过"}'
FULL_PLAN = Plan(True, "安慰一下", ReplyLength.SHORT, "对方难过")


def test_plain_json_is_understood() -> None:
    assert parse_plan(FULL) == FULL_PLAN


def test_json_inside_a_code_fence_and_chatter_is_found() -> None:
    text = "好的，我的建议：\n```json\n" + FULL + "\n```\n希望有帮助"
    assert parse_plan(text) == FULL_PLAN


def test_stray_braces_before_the_object_are_skipped() -> None:
    plan = parse_plan('先说 {这个} 再说 {"reply": false, "reason": "只是晚安"}')
    assert plan == Plan(False, "", None, "只是晚安")


def test_declining_needs_no_intent() -> None:
    plan = parse_plan('{"reply": false}')
    assert plan is not None
    assert plan.should_reply is False
    assert plan.intent == ""


@pytest.mark.parametrize(
    "text",
    [
        "",
        "随便说点什么",
        "{坏掉",
        "[1, 2]",
        '{"reply": "false", "intent": "x"}',
        '{"reply": 0, "intent": "x"}',
        '{"reply": null, "intent": "x"}',
        '{"intent": "x"}',
        '{"reply": true}',
        '{"reply": true, "intent": "   "}',
        '{"reply": true, "intent": 5}',
    ],
)
def test_unreadable_plans_are_none(text: str) -> None:
    # 看不懂一律当没有规划：心跳会合并成一次调用照常回复，不会因此不说话。
    assert parse_plan(text) is None


def test_a_plan_that_is_too_long_is_not_trusted() -> None:
    assert parse_plan(FULL + " " * 5000) is None


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("短", ReplyLength.SHORT),
        ("中", ReplyLength.MEDIUM),
        ("长", ReplyLength.LONG),
        ("Short", ReplyLength.SHORT),
        (" LONG ", ReplyLength.LONG),
        ("很长很长", None),
        ("", None),
    ],
)
def test_length_words(written: str, expected: ReplyLength | None) -> None:
    plan = parse_plan('{"reply": true, "intent": "x", "length": "' + written + '"}')
    assert plan is not None
    assert plan.length is expected


def test_a_length_that_is_not_text_is_ignored() -> None:
    plan = parse_plan('{"reply": true, "intent": "x", "length": 3}')
    assert plan is not None
    assert plan.length is None


def test_long_fields_are_cut_and_flattened_to_one_line() -> None:
    raw = {"reply": True, "intent": "字" * 1000, "reason": "第一行\n第二行"}
    plan = parse_plan(json.dumps(raw, ensure_ascii=False))
    assert plan is not None
    assert len(plan.intent) == 300
    assert plan.reason == "第一行 第二行"


@pytest.mark.parametrize(
    ("length", "limit", "expected"),
    [
        (ReplyLength.LONG, ReplyLength.SHORT, ReplyLength.SHORT),
        (ReplyLength.LONG, ReplyLength.MEDIUM, ReplyLength.MEDIUM),
        (ReplyLength.SHORT, ReplyLength.MEDIUM, ReplyLength.SHORT),
        (ReplyLength.MEDIUM, ReplyLength.MEDIUM, ReplyLength.MEDIUM),
        (ReplyLength.LONG, None, ReplyLength.LONG),
    ],
)
def test_length_is_capped_by_the_tier_limit(
    length: ReplyLength, limit: ReplyLength | None, expected: ReplyLength
) -> None:
    assert length.capped_at(limit) is expected


def test_metadata_is_plain_json() -> None:
    metadata = FULL_PLAN.to_metadata()
    assert metadata == {"reply": True, "intent": "安慰一下", "length": "短", "reason": "对方难过"}
    json.dumps(metadata, ensure_ascii=False)
