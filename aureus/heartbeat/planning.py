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

from aureus.common.limits import MAX_INTEREST_CHARS

_MAX_INTEREST_REASON_CHARS = 100
# D80 第 3 条：召回关键词最多 3 个，每个 2～20 字。
MAX_RECALL_KEYWORDS = 3
MIN_RECALL_KEYWORD_CHARS = 2
MAX_RECALL_KEYWORD_CHARS = 20
_EXPRESSION_INTENT_FIELDS = ("focus", "reply_act", "scene", "tone", "prefer")
_MAX_EXPRESSION_INTENT_CHARS = 80
_MAX_EXPRESSION_INTENT_ITEMS = 3


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

    def lowered(self) -> "ReplyLength":
        """降一档：长变中，中变短，短还是短。"""
        index = _LENGTH_ORDER.index(self)
        return _LENGTH_ORDER[max(index - 1, 0)]


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
# 日程字段（D74 第 3 条）：一次最多处理几项、约定内容截到多长（和 agenda 的上限一致）。
_MAX_RESOLUTIONS = 5
_MAX_COMMIT_CHARS = 200


class ResolutionResult(StrEnum):
    """规划器说某项约定怎么样了。"""

    DONE = "done"  # 做完了
    DROPPED = "dropped"  # 做不了（必须给原因）


@dataclass(frozen=True)
class Commitment:
    """规划器说这次答应了什么。到期时间是原样的文字，由日程按时区解析（D74 第 3 条）。"""

    what: str
    due_text: str | None

    def to_metadata(self) -> dict[str, Any]:
        """写进时间线 metadata 的样子。"""
        return {"what": self.what, "due": self.due_text}


@dataclass(frozen=True)
class Resolution:
    """规划器说某项约定做完了或做不了。"""

    item_id: int
    result: ResolutionResult
    reason: str

    def to_metadata(self) -> dict[str, Any]:
        """写进时间线 metadata 的样子。"""
        return {"id": self.item_id, "result": self.result.value, "reason": self.reason}


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
    # 这次答应了什么（D74）；没有或看不懂时是 None。
    commit: Commitment | None = None
    # 处理了哪几项约定（D74）；看不懂的项直接丢掉。
    resolutions: tuple[Resolution, ...] = ()
    interest: "InterestChange | None" = None
    recall: tuple[str, ...] = ()
    expression_focus: tuple[str, ...] = ()
    expression_avoid: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        """写进时间线 metadata 的样子。"""
        metadata: dict[str, Any] = {
            "reply": self.should_reply,
            "intent": self.intent,
            "length": self.length.value if self.length is not None else None,
            "reason": self.reason,
        }
        if self.commit is not None:
            metadata["commit"] = self.commit.to_metadata()
        if self.resolutions:
            metadata["resolve"] = [item.to_metadata() for item in self.resolutions]
        if self.interest is not None:
            metadata["interest"] = {
                "action": self.interest.action.value,
                "text": self.interest.text,
                "interest_id": self.interest.interest_id,
                "reason": self.interest.reason,
            }
        return metadata


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
    expression_intent = data.get("expression_intent")
    expression_intent = expression_intent if isinstance(expression_intent, dict) else {}
    return Plan(
        should_reply=should_reply,
        intent=intent,
        length=length,
        reason=clean_text(data.get("reason"), _MAX_REASON_CHARS),
        commit=_parse_commit(data.get("commit")),
        resolutions=_parse_resolutions(data.get("resolve")),
        interest=_parse_interest(data.get("interest")),
        recall=_parse_recall(data.get("recall")),
        expression_focus=tuple(
            item
            for field in _EXPRESSION_INTENT_FIELDS
            for item in _expression_terms(expression_intent.get(field))
        ),
        expression_avoid=_expression_terms(expression_intent.get("avoid")),
    )


def _expression_terms(value: object) -> tuple[str, ...]:
    items = value[:_MAX_EXPRESSION_INTENT_ITEMS] if isinstance(value, list) else [value]
    return tuple(filter(None, (clean_text(item, _MAX_EXPRESSION_INTENT_CHARS) for item in items)))


def _parse_recall(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    keywords: list[str] = []
    for item in value:
        text = clean_text(item, len(item))
        if (
            MIN_RECALL_KEYWORD_CHARS <= len(text) <= MAX_RECALL_KEYWORD_CHARS
            and text not in keywords
        ):
            keywords.append(text)
    return tuple(keywords[:MAX_RECALL_KEYWORDS])


@dataclass(frozen=True)
class InitiativePlan:
    """主动联系的建议；真实原因另外由代码提供（D76）。"""

    reach_out: bool
    intent: str
    reason: str

    def to_metadata(self) -> dict[str, Any]:
        """写进考虑记录的规划器原意。"""
        return {"reach_out": self.reach_out, "intent": self.intent, "reason": self.reason}


def parse_initiative_plan(text: str) -> InitiativePlan | None:
    """读懂主动建议；拿不准就不打扰，与用户已经开口时的兜底方向相反（D76）。"""
    if len(text) > _MAX_PLAN_CHARS:
        return None
    data = first_json_object(text)
    if data is None or not isinstance(data.get("reach_out"), bool):
        return None
    reach_out = data["reach_out"]
    intent = clean_text(data.get("intent"), _MAX_INTENT_CHARS)
    if reach_out and not intent:
        return None
    return InitiativePlan(reach_out, intent, clean_text(data.get("reason"), _MAX_REASON_CHARS))


class InterestAction(StrEnum):
    """用户私聊里可提出的一项兴趣改动（D79）。"""

    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"


@dataclass(frozen=True)
class InterestChange:
    """通过形状校验的兴趣建议；写入前还要核对来源和每日限额（D79）。"""

    action: InterestAction
    text: str | None
    interest_id: int | None
    reason: str


def _parse_interest(value: object) -> InterestChange | None:
    if not isinstance(value, dict):
        return None
    try:
        action = InterestAction(value.get("action"))
    except (ValueError, TypeError):
        return None
    text, reason, interest_id = value.get("text"), value.get("reason"), value.get("interest_id")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= _MAX_INTEREST_REASON_CHARS:
        return None
    if action is not InterestAction.REMOVE and (
        not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_INTEREST_CHARS
    ):
        return None
    if action is not InterestAction.ADD and (type(interest_id) is not int or interest_id < 1):
        return None
    return InterestChange(
        action,
        text.strip() if isinstance(text, str) else None,
        interest_id if type(interest_id) is int else None,
        reason.strip(),
    )


def _parse_commit(value: object) -> Commitment | None:
    """可选的 commit 字段；看不懂就当没有，不影响规划本身（D74 第 3 条）。"""
    if not isinstance(value, dict):
        return None
    what = clean_text(value.get("what"), _MAX_COMMIT_CHARS)
    if not what:
        return None
    due = value.get("due")
    return Commitment(what=what, due_text=due.strip() if isinstance(due, str) else None)


def _parse_resolutions(value: object) -> tuple[Resolution, ...]:
    """可选的 resolve 字段：只留看得懂的项，取前 5 个；放弃却没写原因的不收（D74 第 6 条）。"""
    if not isinstance(value, list):
        return ()
    results: list[Resolution] = []
    for entry in value:
        if len(results) >= _MAX_RESOLUTIONS:
            break
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("id")
        result_text = entry.get("result")
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            continue
        if not isinstance(result_text, str):
            continue
        try:
            result = ResolutionResult(result_text.strip().lower())
        except ValueError:
            continue
        reason = clean_text(entry.get("reason"), _MAX_REASON_CHARS)
        if result is ResolutionResult.DROPPED and not reason:
            continue
        results.append(Resolution(item_id, result, reason))
    return tuple(results)


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
