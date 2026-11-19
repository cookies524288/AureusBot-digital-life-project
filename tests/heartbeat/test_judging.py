"""判断器官报告的解析测试（路线图 2.2，D68）：任何一处不合格都整次作废，返回 None。"""

import json
from typing import Any

import pytest

from aureus.heartbeat.judging import (
    SENDER_SUBJECT,
    Judgment,
    OtherEmotion,
    OtherEmotionKind,
    TowardMe,
    TowardMeKind,
    parse_judgment,
)

OTHER = {"subject": "sender", "emotion": "hurt", "intensity": 0.6, "confidence": 0.8}
TOWARD = {"kind": "offense", "intensity": 0.7, "confidence": 0.9}
FULL = json.dumps({"others": [OTHER], "toward_me": [TOWARD]})
FULL_JUDGMENT = Judgment(
    others=(OtherEmotion(SENDER_SUBJECT, OtherEmotionKind.HURT, 0.6, 0.8),),
    toward_me=(TowardMe(TowardMeKind.OFFENSE, 0.7, 0.9),),
)


def report(others: list[Any] | Any = None, toward_me: list[Any] | Any = None) -> str:
    """拼一份判断；不给的列表当成空的。"""
    return json.dumps(
        {
            "others": [] if others is None else others,
            "toward_me": [] if toward_me is None else toward_me,
        }
    )


def test_plain_json_is_understood() -> None:
    assert parse_judgment(FULL) == FULL_JUDGMENT


def test_json_inside_a_code_fence_and_chatter_is_found() -> None:
    text = "判断如下：\n```json\n" + FULL + "\n```\n以上"
    assert parse_judgment(text) == FULL_JUDGMENT


def test_empty_lists_mean_judged_and_nothing_to_report() -> None:
    assert parse_judgment(report()) == Judgment(others=(), toward_me=())


def test_each_enum_value_is_accepted() -> None:
    others = [{**OTHER, "emotion": kind.value} for kind in OtherEmotionKind]
    toward_me = [{**TOWARD, "kind": kind.value} for kind in TowardMeKind]
    judgment = parse_judgment(report(others=others, toward_me=toward_me))
    assert judgment is not None
    assert [item.emotion for item in judgment.others] == list(OtherEmotionKind)
    assert [item.kind for item in judgment.toward_me] == list(TowardMeKind)


def test_enum_values_ignore_case_and_surrounding_spaces() -> None:
    judgment = parse_judgment(
        report(others=[{**OTHER, "emotion": " Anger "}], toward_me=[{**TOWARD, "kind": "PRAISE"}])
    )
    assert judgment is not None
    assert judgment.others[0].emotion is OtherEmotionKind.ANGER
    assert judgment.toward_me[0].kind is TowardMeKind.PRAISE


def test_boundary_numbers_and_integers_are_accepted() -> None:
    judgment = parse_judgment(report(others=[{**OTHER, "intensity": 0, "confidence": 1}]))
    assert judgment is not None
    assert (judgment.others[0].intensity, judgment.others[0].confidence) == (0.0, 1.0)
    assert isinstance(judgment.others[0].intensity, float)


def test_subject_is_cleaned_and_cut_to_forty_characters() -> None:
    long_name = "小" * 60
    judgment = parse_judgment(
        report(others=[{**OTHER, "subject": f"  {long_name}\n"}, {**OTHER, "subject": "小  猫"}])
    )
    assert judgment is not None
    assert judgment.others[0].subject == "小" * 40
    assert judgment.others[1].subject == "小 猫"


def test_five_items_are_allowed() -> None:
    judgment = parse_judgment(report(others=[OTHER] * 5, toward_me=[TOWARD] * 5))
    assert judgment is not None
    assert (len(judgment.others), len(judgment.toward_me)) == (5, 5)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "没什么情绪",
        "{坏掉",
        "[1, 2]",
        '"others"',
        "{}",
        '{"others": []}',  # 缺 toward_me
        '{"toward_me": []}',  # 缺 others
        '{"others": null, "toward_me": []}',
        '{"others": {}, "toward_me": []}',  # 不是列表
        '{"others": "none", "toward_me": []}',
        '{"others": [], "toward_me": 0}',
    ],
)
def test_unreadable_or_incomplete_reports_are_discarded(text: str) -> None:
    assert parse_judgment(text) is None


def test_too_many_items_discard_the_whole_report() -> None:
    assert parse_judgment(report(others=[OTHER] * 6)) is None
    assert parse_judgment(report(toward_me=[TOWARD] * 6)) is None


def test_output_over_four_thousand_characters_is_discarded() -> None:
    assert parse_judgment(FULL + " " * 4000) is None


@pytest.mark.parametrize(
    "bad",
    [
        {**OTHER, "emotion": "fear"},  # 不在列表里
        {**OTHER, "emotion": "愉悦"},  # 只认代码里的英文名
        {**OTHER, "emotion": 3},
        {**OTHER, "emotion": None},
        {**OTHER, "subject": ""},
        {**OTHER, "subject": "   "},
        {**OTHER, "subject": 5},
        {**OTHER, "intensity": 1.01},
        {**OTHER, "intensity": -0.01},
        {**OTHER, "confidence": 2},
        {**OTHER, "intensity": True},  # 布尔值不算数字
        {**OTHER, "confidence": False},
        {**OTHER, "intensity": "0.5"},
        {**OTHER, "intensity": None},
        {"subject": "sender", "emotion": "joy", "intensity": 0.5},  # 缺 confidence
        {"emotion": "joy", "intensity": 0.5, "confidence": 0.5},  # 缺 subject
        "joy",
        ["joy"],
        None,
    ],
)
def test_one_bad_other_emotion_discards_the_whole_report(bad: Any) -> None:
    # 好的那项放前面：不合格的只要有一项，连好的也不用。
    assert parse_judgment(report(others=[OTHER, bad], toward_me=[TOWARD])) is None


@pytest.mark.parametrize(
    "bad",
    [
        {**TOWARD, "kind": "surprise"},  # 惊讶只来自反馈
        {**TOWARD, "kind": "anger"},
        {**TOWARD, "kind": ""},
        {**TOWARD, "kind": 1},
        {**TOWARD, "intensity": 1.5},
        {**TOWARD, "confidence": -1},
        {**TOWARD, "intensity": True},
        {**TOWARD, "confidence": "high"},
        {"kind": "praise", "confidence": 0.5},  # 缺 intensity
        {"intensity": 0.5, "confidence": 0.5},  # 缺 kind
        "praise",
        None,
    ],
)
def test_one_bad_toward_me_item_discards_the_whole_report(bad: Any) -> None:
    assert parse_judgment(report(others=[OTHER], toward_me=[TOWARD, bad])) is None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_discarded(literal: str) -> None:
    # Python 的 json 默认认得 NaN 和 Infinity，不能让它们混进强度。
    text = (
        '{"others": [{"subject": "sender", "emotion": "joy", '
        f'"intensity": {literal}, "confidence": 0.5}}], "toward_me": []}}'
    )
    assert parse_judgment(text) is None


def test_metadata_has_plain_values_that_survive_json() -> None:
    judgment = parse_judgment(FULL)
    assert judgment is not None
    metadata = judgment.to_metadata()
    assert metadata == {"others": [OTHER], "toward_me": [TOWARD]}
    assert json.loads(json.dumps(metadata)) == metadata
