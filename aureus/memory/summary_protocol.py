"""长期总结四段协议；检查原件、确认方式、端点和更正，不替代语义判断。"""

import json
import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_MESSAGE_CHARS = 1600
_MAX_FACTS = 16
_MAX_SUMMARY = 1500
_NON_AUTHORED = re.compile(
    r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`\n]+`|^\s*>[^\n]*|"
    r"^\s*[“「『\"][^\n]*[”」』\"]\s*$|"
    r'^\s*(?:\{\s*"|\[\s*["{\[])[^\n]*|'
    r"(?:转述|转发|引用|他说|她说|别人说)\s*[:：]\s*(?:[“「『\"][^\n]*?[”」』\"]|[^\n]*)",
    re.MULTILINE | re.DOTALL,
)
_NOISE = re.compile(
    r"玩笑|猜测|听说|据说|转述|转发|(?:他说|她说|别人说)|例如|举例|示例|假设|工具输出|"
    r"注入|忽略.{0,8}指令|角色扮演|可能|或许|印象里|我感觉|我觉得|我以为|我怀疑|"
    r"只是测试|仅供测试|演示数据|不要记|别记|不是真的|不是事实"
)
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?", re.UNICODE)
_CONFIRMATION = re.compile(r"(?:对(?:的|没错)?|是的|确实|确认|说得对|没错|属实)[。！!，,\s]*$")
_CORRECTION = re.compile(r"更正|纠正|改为|改成|换成|搬到|准确来说|而是")
_BOUNDARY_PUNCTUATION = "。！？!?；;，, \r\n"

type SummaryScope = tuple[str, str]
type Nonempty = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
type FactText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]


@dataclass(frozen=True)
class SummaryMessage:
    """平台原件的临时视图；被引用消息不是当前发言者写的正文。"""

    sequence: int
    message_id: str
    sender_id: str
    sender_name: str
    content: str
    is_human: bool
    quoted_message_id: str | None = None
    images: tuple[str, ...] = ()

    @property
    def authored_text(self) -> str:
        """保留本来就在消息中的正文，不把引用、代码或截断尾段拼接成证据。"""
        return _NON_AUTHORED.sub(lambda match: "\n" * len(match[0]), self.content)[
            :MAX_MESSAGE_CHARS
        ]

    @property
    def can_be_evidence(self) -> bool:
        """报告是否存在可用人类正文；具体片段仍要复核。"""
        return self.is_human and any(_plain_statements(self))


@dataclass(frozen=True)
class PreviousFact:
    """同域仍有效的旧事实，仅用于判重和定位明确的更正。"""

    identifier: int
    text: str
    sender_id: str
    predicate: str
    value: str


@dataclass(frozen=True)
class SummaryWindow:
    """由同一账号、会话和可见范围限定的一段新原件。"""

    scope: SummaryScope
    start: int
    messages: tuple[SummaryMessage, ...]
    previous_summaries: tuple[str, ...] = ()
    previous_facts: tuple[PreviousFact, ...] = ()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ImageEvidence(_Strict):
    """图片必须以原消息 ID 和组件路径共同定位。"""

    message_id: Nonempty
    component_path: Nonempty


class _Source(_Strict):
    message_id: Nonempty
    sender_id: Nonempty
    excerpt: FactText


class SummaryFact(_Strict):
    """人类确认来源与可选的前序材料；片段只用于验证，不持久化。"""

    fact_id: Nonempty
    text: FactText
    predicate: Nonempty
    value: Nonempty
    source: _Source
    image_evidence: list[ImageEvidence] = Field(max_length=_MAX_FACTS)
    context: _Source | None = None
    shared: bool = False
    replaces: Annotated[int, Field(gt=0)] | None = None

    @property
    def claim_owner(self) -> str:
        """个人事实沿用稳定发言者，公共事件或知识不挂在某个昵称上。"""
        return "" if self.shared else self.source.sender_id


class SummaryRelation(_Strict):
    """关系端点必须可从已确认事实及其来源找到。"""

    subject: Nonempty
    predicate: Nonempty
    object: Nonempty


class SummaryDelta(_Strict):
    """模型提议的四段增量，明确的四空视为一次成功。"""

    summary: str = Field(max_length=_MAX_SUMMARY)
    entities: list[Nonempty] = Field(max_length=_MAX_FACTS * 2)
    relations: list[SummaryRelation] = Field(max_length=_MAX_FACTS)
    facts: list[SummaryFact] = Field(max_length=_MAX_FACTS)


def _plain_statements(message: SummaryMessage, *, context: bool = False) -> tuple[str, ...]:
    return tuple(
        part[0].strip()
        for part in _SENTENCE.finditer(message.authored_text)
        if part[0].strip()
        and not _NOISE.search(part[0])
        and (context or not part[0].rstrip().endswith(("?", "？")))
    )


def _denies(text: str, value: str) -> bool:
    escaped = re.escape(value)
    return bool(
        re.search(
            rf"(?:不是|并非|不再|取消|原先是)"
            rf"(?:(?!而是|实际是|改为|改成)[^，,。！？；;]){{0,24}}{escaped}"
            rf"|{escaped}[^，,。！？；;]{{0,16}}(?:改为|改成|换成|取消)",
            text,
        )
    )


def _locate(
    ref: _Source,
    material: dict[str, SummaryMessage],
    *,
    context: bool = False,
) -> tuple[SummaryMessage, tuple[str, ...]]:
    message = material.get(ref.message_id)
    if message is None or message.sender_id != ref.sender_id:
        raise ValueError("证据消息或发言者不在当前窗口")
    if not context and not message.is_human:
        raise ValueError("机器人发言不能独立确认事实")
    parts = tuple(
        part for part in _plain_statements(message, context=context) if ref.excerpt in part
    )
    if not parts:
        raise ValueError("证据片段不在可用正文中")
    return message, parts


def _has_final_value(excerpt: str, parts: tuple[str, ...], value: str) -> bool:
    return value in excerpt and all(not _denies(part, value) for part in parts)


def _is_copy(text: str, message: SummaryMessage) -> bool:
    original = " ".join(message.content.split()).strip(_BOUNDARY_PUNCTUATION)
    derived = " ".join(text.split()).strip(_BOUNDARY_PUNCTUATION)
    return bool(original and (original == derived or (len(original) >= 24 and original in derived)))


def _validate_fact(
    fact: SummaryFact,
    window: SummaryWindow,
    material: dict[str, SummaryMessage],
) -> tuple[SummaryMessage, set[str], set[str]]:
    source, parts = _locate(fact.source, material)
    grounded_value = _has_final_value(fact.source.excerpt, parts, fact.value)
    evidence = {source.message_id: source}
    anchors = {fact.source.excerpt, source.sender_name}
    if fact.context is not None:
        context, earlier = _locate(fact.context, material, context=True)
        linked = source.quoted_message_id == context.message_id or (
            source.quoted_message_id is None
            and context.sequence
            == max(
                (item.sequence for item in window.messages if item.sequence < source.sequence),
                default=-1,
            )
        )
        if context.sequence >= source.sequence or not linked:
            raise ValueError("确认无法定位到前序发言")
        if not grounded_value:
            grounded_value = (
                any(_CONFIRMATION.fullmatch(part) for part in parts)
                and (fact.shared or (context.is_human and context.sender_id == source.sender_id))
                and _has_final_value(fact.context.excerpt, earlier, fact.value)
            )
            if grounded_value:
                anchors.add(fact.context.excerpt)
        elif context.is_human and _has_final_value(fact.context.excerpt, earlier, fact.value):
            anchors.add(fact.context.excerpt)
        evidence[context.message_id] = context
        if context.is_human:
            anchors.add(context.sender_name)
    if not grounded_value or fact.value not in fact.text or _NOISE.search(fact.text):
        raise ValueError("事实缺少可定位的人类确认")
    if any(_is_copy(fact.text, item) for item in window.messages):
        raise ValueError("事实复述了消息原文")
    paths = {(image.message_id, image.component_path) for image in fact.image_evidence}
    if len(paths) != len(fact.image_evidence) or not paths <= {
        (item.message_id, path)
        for item in evidence.values()
        if item.is_human
        for path in item.images
    }:
        raise ValueError("图片不在该事实已引用的真实附件中")
    retired = _validate_corrections(fact, source, parts, window)
    return source, anchors, retired


def _validate_corrections(
    fact: SummaryFact,
    source: SummaryMessage,
    parts: tuple[str, ...],
    window: SummaryWindow,
) -> set[str]:
    old = [
        item
        for item in window.previous_facts
        if item.sender_id == fact.claim_owner and item.predicate == fact.predicate
    ]
    if any(item.value == fact.value for item in old):
        raise ValueError("历史事实没有变化")
    changed = any(_CORRECTION.search(part) for part in parts)
    retired = {item.value for item in old if item.identifier == fact.replaces}
    ambiguous = len(old) > 1 and not any(
        _denies(part, value) for part in parts for value in retired
    )
    if (fact.replaces is not None and (not changed or not retired or ambiguous)) or (
        fact.replaces is None and old and changed
    ):
        raise ValueError("没有明确定位到同一项既有事实的更正")
    for item in window.messages:
        if not item.is_human or (not fact.shared and item.sender_id != source.sender_id):
            continue
        for part in _plain_statements(item):
            if item.sequence > source.sequence and (
                _denies(part, fact.value)
                or (item.quoted_message_id == source.message_id and _CORRECTION.search(part))
            ):
                raise ValueError("窗口内较晚的确认已经否定该值")
            if item.sequence == source.sequence and _CORRECTION.search(part):
                for earlier in window.messages:
                    if (
                        earlier.sequence < item.sequence
                        and (fact.shared or earlier.sender_id == item.sender_id)
                        and earlier.is_human
                    ):
                        retired.update(
                            value
                            for value in re.findall(
                                r"(?:不是|并非)\s*([^，,。；;]+?)\s*[，,]?\s*而是", part
                            )
                            if any(value in line for line in _plain_statements(earlier))
                        )
    return retired


def validate_delta(delta: SummaryDelta, window: SummaryWindow) -> None:
    """四空之外的任一项目无效即拒绝整批，防止略过坏输出。"""
    if not delta.facts:
        if delta.summary != "" or delta.entities or delta.relations:
            raise ValueError("没有事实时只接受四空")
        return
    if not delta.summary.strip() or any(_is_copy(delta.summary, item) for item in window.messages):
        raise ValueError("摘要为空或复述消息原文")
    material = {item.message_id: item for item in window.messages}
    fact_ids: set[str] = set()
    claims: set[tuple[str, str, str]] = set()
    replacements: set[int] = set()
    entity_groups: list[set[str]] = []
    retired: set[str] = set()
    entities = set(delta.entities)
    if len(entities) != len(delta.entities):
        raise ValueError("重复实体")
    for fact in delta.facts:
        _, anchors, replaced = _validate_fact(fact, window, material)
        claim = fact.claim_owner, fact.predicate, fact.value
        if fact.fact_id in fact_ids or claim in claims or fact.replaces in replacements:
            raise ValueError("重复事实或竞争的更正")
        claims.add(claim)
        fact_ids.add(fact.fact_id)
        if fact.replaces is not None:
            replacements.add(fact.replaces)
        retired.update(replaced)
        entity_groups.append(
            {
                entity
                for entity in entities
                if entity in fact.text and any(entity in anchor for anchor in anchors)
            }
        )
    if not entities <= set().union(*entity_groups):
        raise ValueError("有无法追溯到已验证事实的实体")
    _reject_multiple_stages(delta.facts, material)
    _validate_relations(delta, entity_groups, retired)


def _reject_multiple_stages(
    facts: list[SummaryFact],
    material: dict[str, SummaryMessage],
) -> None:
    for newer in facts:
        _, parts = _locate(newer.source, material)
        if not any(_CORRECTION.search(part) for part in parts):
            continue
        for older in facts:
            if (
                (older.claim_owner, older.predicate) == (newer.claim_owner, newer.predicate)
                and older.value != newer.value
                and material[older.source.message_id].sequence
                < material[newer.source.message_id].sequence
            ):
                raise ValueError("窗口内同一事实只保留最后确认的值")


def _validate_relations(delta: SummaryDelta, groups: list[set[str]], retired: set[str]) -> None:
    relations: set[tuple[str, str, str]] = set()
    published = [delta.summary, *delta.entities, *(fact.text for fact in delta.facts)]
    for relation in delta.relations:
        triple = relation.subject, relation.predicate, relation.object
        if triple in relations or not any(
            {relation.subject, relation.object} <= group for group in groups
        ):
            raise ValueError("关系端点无证据或重复")
        relations.add(triple)
        published.extend(triple)
    if any(value in text for value in retired for text in published):
        raise ValueError("更正后仍输出被替代的旧值")


def parse_delta(text: str, window: SummaryWindow) -> SummaryDelta:
    """解析严格 JSON，保留合法空与错误的区别。"""

    def unique_properties(pairs: list[tuple[str, object]]) -> dict[str, object]:
        properties = dict(pairs)
        if len(properties) != len(pairs):
            raise ValueError("JSON 存在重复字段")
        return properties

    delta = SummaryDelta.model_validate(json.loads(text, object_pairs_hook=unique_properties))
    validate_delta(delta, window)
    return delta
