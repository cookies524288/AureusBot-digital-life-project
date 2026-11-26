"""D137：at 段与正文里的全名/昵称提及。"""

import pytest

from aureus.heartbeat import Perception, TrustLevel, VisibilityScope
from aureus.heartbeat.perception import mentions_bot


def perception(content: str, *, mentioned: bool = False) -> Perception:
    return Perception(
        "qq",
        "sender",
        content,
        VisibilityScope.GROUP,
        TrustLevel.VERIFIED_CONTACT,
        mentioned=mentioned,
    )


@pytest.mark.parametrize(
    ("content", "names"),
    [
        ("测试助手你好", ["测试助手"]),  # 全名
        ("小绿在吗", ["测试助手", "小绿"]),  # 昵称
        ("Aureus 在吗", ["aureus"]),  # 英文大小写不敏感
        ("你好Aureus", ["Aureus"]),  # 英文名紧挨中文
        ("今天小绿来了", ["小绿"]),  # 中文按子串
        (" 小绿 在吗", ["  小绿  "]),  # 名字两侧空白忽略
    ],
)
def test_mentions_bot_recognizes_at_and_names(content: str, names: list[str]) -> None:
    assert mentions_bot(perception(content), names)


def test_mentioned_flag_short_circuits_name_matching() -> None:
    assert mentions_bot(perception("完全无关", mentioned=True), [])


@pytest.mark.parametrize(
    ("content", "names"),
    [
        ("AureusBot 在吗", ["Aureus"]),  # 英文按词边界
        ("xAureus", ["Aureus"]),
        ("Aureus1", ["Aureus"]),
        ("今天天气不错", ["Aureus", "小绿"]),
        ("小绿在吗", ["", "   "]),  # 空名字跳过
        ("小绿在吗", []),
    ],
)
def test_mentions_bot_ignores_non_matches(content: str, names: list[str]) -> None:
    assert not mentions_bot(perception(content), names)
