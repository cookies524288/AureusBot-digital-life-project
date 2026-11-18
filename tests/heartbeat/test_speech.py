"""说话方式：把一段回复拆成几条短消息的测试（路线图 1.7，D60）。"""

import pytest

from aureus.heartbeat.speech import split_reply

# 拆分时会丢掉的标点和空白；比较内容有没有丢，就忽略它们。
DROPPED = "，,。；;！？!? \n"

SAMPLES = [
    "今天面试被拒了。别难过！我陪你聊聊？",
    "嗯，我想想。好吧",
    "他说“好的。我来。”然后走了。再见",
    "哈哈(≧▽≦)。好的呀",
    "一一一。二二二。三三三。四四四。五五五。六六六",
    "好\n我觉得可以试试看\n行",
]


def split(text: str, max_messages: int = 5, min_chars: int = 2) -> list[str]:
    return split_reply(text, max_messages=max_messages, min_chars=min_chars)


def letters(text: str) -> str:
    return "".join(char for char in text if char not in DROPPED)


def test_sentences_become_separate_messages() -> None:
    assert split("今天面试被拒了。别难过！我陪你聊聊？") == [
        "今天面试被拒了",
        "别难过！",
        "我陪你聊聊？",
    ]


def test_commas_do_not_split() -> None:
    assert split("嗯，我想想。好吧") == ["嗯，我想想", "好吧"]


def test_trailing_marks_are_dropped() -> None:
    assert split("好的。") == ["好的"]
    assert split("好的，") == ["好的"]


def test_one_message_for_max_one() -> None:
    assert split("  好。行。 ", max_messages=1) == ["好。行。"]


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_blank_text_gives_nothing(text: str) -> None:
    assert split(text) == []


def test_only_punctuation_is_kept_as_it_is() -> None:
    assert split("。。。") == ["。。。"]


def test_at_least_one_message_is_required() -> None:
    with pytest.raises(ValueError, match="至少是 1"):
        split("好", max_messages=0)


def test_a_too_short_sentence_joins_the_next_one() -> None:
    assert split("好。我觉得可以试试看", min_chars=3) == ["好，我觉得可以试试看"]


def test_a_too_short_last_sentence_joins_the_previous_one() -> None:
    assert split("我觉得可以试试看。嗯", min_chars=3) == ["我觉得可以试试看，嗯"]


def test_a_newline_is_a_hard_boundary_for_short_sentences() -> None:
    assert split("好\n我觉得可以试试看", min_chars=3) == ["好", "我觉得可以试试看"]


def test_quotes_are_not_split_inside() -> None:
    assert split("他说“好的。我来。”然后走了。再见") == ["他说“好的。我来。”然后走了", "再见"]


def test_kaomoji_is_not_broken() -> None:
    assert split("哈哈(≧▽≦)。好的呀") == ["哈哈(≧▽≦)", "好的呀"]
    assert split("好吧(。_。)我知道了") == ["好吧(。_。)我知道了"]


def test_over_the_limit_sentences_are_merged_evenly() -> None:
    text = "一一一。二二二。三三三。四四四。五五五"
    assert split(text, max_messages=2, min_chars=1) == ["一一一，二二二，三三三", "四四四，五五五"]


def test_merging_after_an_exclamation_adds_no_comma() -> None:
    assert split("好呀！我来。就这样", max_messages=2, min_chars=1) == ["好呀！我来", "就这样"]


def test_the_limit_beats_a_newline_boundary() -> None:
    assert split("甲甲甲\n乙乙乙\n丙丙丙", max_messages=2, min_chars=1) == [
        "甲甲甲，乙乙乙",
        "丙丙丙",
    ]


@pytest.mark.parametrize("text", SAMPLES)
def test_nothing_is_lost_and_the_limit_holds(text: str) -> None:
    for limit in (1, 2, 3):
        result = split(text, max_messages=limit)
        assert 1 <= len(result) <= limit
        assert letters("".join(result)) == letters(text)


@pytest.mark.parametrize("text", SAMPLES)
def test_same_text_always_splits_the_same_way(text: str) -> None:
    assert [split(text) for _ in range(5)] == [split(text)] * 5
