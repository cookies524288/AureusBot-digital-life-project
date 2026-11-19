"""规划器的建议：读懂它，读不懂就当没有（03 号文档「器官」，路线图 1.6，D59）。

规划器只返回一段 JSON 文字，说回不回、想表达什么、多长、为什么。这里把它解析成 Plan；
格式不对、缺字段、类型不对都返回 None，由心跳改走「合并成一次调用」照常回复：
规划器坏了不能让它不说话。不确定时朝「回复」倒，只有明确写了不回才不回。
这里只解析文字，不读写任何状态。
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ReplyLength(StrEnum):
    """回复多长。值是写进提示词和时间线的中文。"""

    SHORT = "短"
    MEDIUM = "中"
    LONG = "长"

    def describe(self) -> str:
        """写进提示词里的长度要求。"""
        return _LENGTH_DESCRIPTIONS[self]

    def capped_at(self, limit: "ReplyLength | None") -> "ReplyLength":
        """不超过 limit 的长度；limit 为 None 表示不限。"""
        if limit is None or _LENGTH_ORDER.index(self) <= _LENGTH_ORDER.index(limit):
            return self
        return limit


_LENGTH_ORDER = (ReplyLength.SHORT, ReplyLength.MEDIUM, ReplyLength.LONG)
_LENGTH_DESCRIPTIONS = {
    ReplyLength.SHORT: "一两句话",
    ReplyLength.MEDIUM: "三五句话",
    ReplyLength.LONG: "可以展开说，但别写成长文",
}
# 规划器可能写中文，也可能写英文，两种都认。
_LENGTH_BY_TEXT = {
    **{length.value: length for length in ReplyLength},
    "short": ReplyLength.SHORT,
    "medium": ReplyLength.MEDIUM,
    "long": ReplyLength.LONG,
}

# 规划器的输出超过这么多字就不当成建议：正常的建议只有几十个字，太长说明它没按格式写，
# 也免得在一大段文字里反复找 JSON。
_MAX_PLAN_CHARS = 4000
# 意图和理由会写进回复器的提示词和时间线，截到这么长，免得一条建议把上下文撑满。
_MAX_INTENT_CHARS = 300
_MAX_REASON_CHARS = 200


@dataclass(frozen=True)
class Plan:
    """读懂了的规划器建议。"""

    # 要不要回复。
    should_reply: bool
    # 想表达的意思；要回复时一定不是空的。
    intent: str
    # 建议的长度；规划器没写或写得看不懂时是 None，由档位决定。
    length: ReplyLength | None
    # 为什么这样决定；可以是空的。
    reason: str

    def to_metadata(self) -> dict[str, Any]:
        """写进时间线 metadata 的样子。"""
        return {
            "reply": self.should_reply,
            "intent": self.intent,
            "length": self.length.value if self.length is not None else None,
            "reason": self.reason,
        }


def parse_plan(text: str) -> Plan | None:
    """把规划器的输出解析成建议。

    认得出前后有别的文字、或者包在 ```json 代码块里的 JSON 对象。

    Args:
        text: 规划器返回的文字。

    Returns:
        读懂了返回 Plan；读不懂（没有 JSON 对象、reply 不是 true/false、要回复却没写意图）
        返回 None。
    """
    if len(text) > _MAX_PLAN_CHARS:
        return None
    data = first_json_object(text)
    if data is None:
        return None
    should_reply = data.get("reply")
    # 只认真正的 true/false：写成 "false" 字符串或 0 时不猜，当看不懂，照常回复。
    if not isinstance(should_reply, bool):
        return None
    intent = clean_text(data.get("intent"), _MAX_INTENT_CHARS)
    if should_reply and not intent:
        return None
    length_text = data.get("length")
    length = (
        _LENGTH_BY_TEXT.get(length_text.strip().lower()) if isinstance(length_text, str) else None
    )
    return Plan(
        should_reply=should_reply,
        intent=intent,
        length=length,
        reason=clean_text(data.get("reason"), _MAX_REASON_CHARS),
    )


def first_json_object(text: str) -> dict[str, Any] | None:
    """找出文字里第一个能解析的 JSON 对象。"""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _end = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        return value if isinstance(value, dict) else None
    return None


def clean_text(value: object, limit: int) -> str:
    """去掉首尾空白，压成一行，截到 limit 个字；不是字符串时当成空的。"""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]
