"""判断器官的报告：读懂它，读不懂就当没判断出来（03 号文档「器官」，路线图 2.2，D68）。

判断器官只返回一段 JSON 文字，报两类事：别人的情绪（给同理心用），冲着它来的事（给激素用）。
这里把它解析成 Judgment；任何一处不合格都整次作废返回 None，不挑着用：
部分格式错误说明模型没按要求理解任务，剩下的也不可信（D68 第 2 条）。
这里只解析文字，不读写任何状态。
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from aureus.heartbeat.planning import clean_text, first_json_object

# 对象写成这个词，表示情绪属于发消息的人本人。
SENDER_SUBJECT = "sender"

# 输出超过这么多字就不当成报告：正常的报告只有几百字（和规划器同一个理由）。
_MAX_JUDGMENT_CHARS = 4000
# 每个列表最多这么多项：一条消息里值得报告的情绪不会更多，更多说明它在凑数。
_MAX_ITEMS = 5
# 对象的称呼会写进时间线，截到这么长。
_MAX_SUBJECT_CHARS = 40


class OtherEmotionKind(StrEnum):
    """别人的情绪，只能是激素里的四种短期情绪（09 号文档术语表）。"""

    ANGER = "anger"
    JOY = "joy"
    HURT = "hurt"
    SURPRISE = "surprise"


class TowardMeKind(StrEnum):
    """冲着它来的事，种类由代码固定（D68 第 1 条）。惊讶只来自反馈，不在这里。"""

    OFFENSE = "offense"  # 羞辱、戏弄、嘲笑，对应怒气
    PRAISE = "praise"  # 夸它，对应愉悦
    REJECTION = "rejection"  # 拒绝或冷落它，对应低落


@dataclass(frozen=True)
class OtherEmotion:
    """判断出的某个人或动物的情绪。"""

    # "sender" 或消息里提到的人、动物的称呼。
    subject: str
    emotion: OtherEmotionKind
    # 强度和把握都是 0 到 1。
    intensity: float
    confidence: float

    def to_metadata(self) -> dict[str, Any]:
        """写进时间线 metadata 的样子。"""
        return {
            "subject": self.subject,
            "emotion": self.emotion.value,
            "intensity": self.intensity,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class TowardMe:
    """判断出的冲着它来的事。"""

    kind: TowardMeKind
    intensity: float
    confidence: float

    def to_metadata(self) -> dict[str, Any]:
        """写进时间线 metadata 的样子。"""
        return {
            "kind": self.kind.value,
            "intensity": self.intensity,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Judgment:
    """读懂了的判断。两个元组都可以是空的，表示判断了，没什么情绪。"""

    others: tuple[OtherEmotion, ...]
    toward_me: tuple[TowardMe, ...]

    def to_metadata(self) -> dict[str, Any]:
        """写进时间线 metadata 的样子。"""
        return {
            "others": [item.to_metadata() for item in self.others],
            "toward_me": [item.to_metadata() for item in self.toward_me],
        }


def parse_judgment(text: str) -> Judgment | None:
    """把判断器官的输出解析成判断。

    认得出前后有别的文字、或者包在 ```json 代码块里的 JSON 对象。

    Args:
        text: 判断器官返回的文字。

    Returns:
        读懂了返回 Judgment；读不懂返回 None：输出太长、没有 JSON 对象、缺 others 或 toward_me、
        不是列表、超过 5 项、有一项缺字段、情绪或种类不在列表里、数值不是 0 到 1 之间的数字。
    """
    if len(text) > _MAX_JUDGMENT_CHARS:
        return None
    data = first_json_object(text)
    if data is None:
        return None
    raw_others = _item_list(data.get("others"))
    raw_toward_me = _item_list(data.get("toward_me"))
    if raw_others is None or raw_toward_me is None:
        return None
    others = [_parse_other(item) for item in raw_others]
    toward_me = [_parse_toward_me(item) for item in raw_toward_me]
    if any(item is None for item in others) or any(item is None for item in toward_me):
        return None
    return Judgment(
        others=tuple(item for item in others if item is not None),
        toward_me=tuple(item for item in toward_me if item is not None),
    )


def _item_list(value: object) -> list[object] | None:
    """列表且不超过上限时原样返回，否则 None。"""
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        return None
    return value


def _parse_other(item: object) -> OtherEmotion | None:
    if not isinstance(item, dict):
        return None
    subject = clean_text(item.get("subject"), _MAX_SUBJECT_CHARS)
    emotion = _enum_value(OtherEmotionKind, item.get("emotion"))
    intensity = _unit_number(item.get("intensity"))
    confidence = _unit_number(item.get("confidence"))
    if not subject or emotion is None or intensity is None or confidence is None:
        return None
    return OtherEmotion(subject, emotion, intensity, confidence)


def _parse_toward_me(item: object) -> TowardMe | None:
    if not isinstance(item, dict):
        return None
    kind = _enum_value(TowardMeKind, item.get("kind"))
    intensity = _unit_number(item.get("intensity"))
    confidence = _unit_number(item.get("confidence"))
    if kind is None or intensity is None or confidence is None:
        return None
    return TowardMe(kind, intensity, confidence)


def _enum_value[E: StrEnum](enum_type: type[E], value: object) -> E | None:
    """字符串（忽略大小写和首尾空白）对上枚举成员就返回它，否则 None。"""
    if not isinstance(value, str):
        return None
    try:
        return enum_type(value.strip().lower())
    except ValueError:
        return None


def _unit_number(value: object) -> float | None:
    """0 到 1 之间的数字。布尔值不算数字，NaN 和无穷大也不行。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number
