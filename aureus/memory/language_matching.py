"""习得语言检索。

黑话匹配移植自 MaiBot 1.3.15 的 src/maisaka/jargon_context_matcher.py（GPL-3.0）。
保留规范化、子串匹配、位置和频率得分。统一库按词和释义去重，保留各义的证据。
来源：https://github.com/Mai-with-u/MaiBot
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from aureus.common.language_prompts import (
    LANGUAGE_EXPRESSION_HEADER,
    LANGUAGE_EXPRESSION_ITEM,
    LANGUAGE_JARGON_HEADER,
    LANGUAGE_JARGON_ITEM,
    LANGUAGE_REFERENCE_NOTE,
    language_reference_block,
)

MAX_JARGON_MATCHES = 10
MAX_EXPRESSION_MATCHES = 3
MIN_JARGON_EVIDENCE = 4  # MaiBot 首次推断的门槛。
_PLANNER_MESSAGE_PREFIX = re.compile(r"^<message\b[^>]*>\s*", re.IGNORECASE)
_TERMS = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")


class ExpressionReviewStatus(StrEnum):
    """表达方式的人工审核结论，与启用状态分别保存。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class JargonStatus(StrEnum):
    """黑话定性与释义、启停或召回授权相互独立。"""

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    NON_JARGON = "non_jargon"


@dataclass(frozen=True)
class LanguageEntry:
    """一项候选及其可定位的本地消息证据。"""

    text: str
    detail: str
    example: str
    count: int
    evidence_ids: tuple[int, ...]
    speakers: frozenset[str]
    source_refs: tuple[str, ...] = ()
    entry_key: str = ""
    is_enabled: bool = True
    original: tuple[str, str, str] | None = None
    pending_scope: bool = False
    is_removed: bool = False
    review_status: ExpressionReviewStatus | None = None
    scope_keys: tuple[str, ...] = ()
    package_source: str | None = None
    learning_source: str | None = None
    restricted_scope: str | None = None
    jargon_status: JargonStatus | None = None

    def visible_in(self, conversation_key: str | None) -> bool:
        """在线来源仅用于管理筛选；导入包仍遵守显式授权。"""
        return (self.restricted_scope is None or conversation_key == self.restricted_scope) and (
            self.package_source is None or conversation_key in self.scope_keys
        )

    def can_reference_jargon(self) -> bool:
        """义项与旧人工定性分开；只有可用的非空义项才能成为参考。"""
        return bool(self.detail.strip()) and self.is_enabled and not self.is_removed


@dataclass(frozen=True)
class TermFrequency:
    """原始高频词排名与计数参与黑话加权。"""

    rank: int
    count: int


def normalize(text: str) -> str:
    """规范化文本的大小写和空白。"""
    return " ".join(text.strip().lower().split())


def message_text(text: str) -> str:
    """剥掉上游 Planner 消息头，保留正文。"""
    return _PLANNER_MESSAGE_PREFIX.sub("", text.strip(), count=1)


def is_reference(text: str) -> bool:
    """学习时排除导出的黑话参考块。"""
    normalized = message_text(text)
    first_lines = normalized.splitlines()[:2]
    return (
        normalized.startswith(("<jargon>", "以下释义来自当前上下文中部分词汇的猜测含义"))
        or bool(
            first_lines
            and first_lines[0].startswith(
                (
                    LANGUAGE_JARGON_HEADER.default.template,
                    LANGUAGE_EXPRESSION_HEADER.default.template,
                )
            )
        )
        or LANGUAGE_REFERENCE_NOTE.default.template in first_lines
    )


def _jargon_score(count: int, index: int, frequency: TermFrequency | None) -> float:
    bonus = 0.0
    if frequency is not None:
        bonus = 1000.0 + frequency.count * 2.0
        if frequency.rank > 0:
            bonus += max(0.0, 100.0 - frequency.rank)
    return count + bonus - index * 0.01


def match_jargons(
    texts: Sequence[str],
    entries: Sequence[LanguageEntry],
    frequencies: dict[str, TermFrequency],
) -> list[LanguageEntry]:
    """按最近人类消息的位置、词频和用例数选黑话，保留同词不同义。"""
    normalized_texts = [normalize(message_text(text)) for text in texts]
    matches: list[tuple[float, int, LanguageEntry]] = []
    for entry in entries:
        term = normalize(entry.text)
        if not term or not entry.can_reference_jargon():
            continue
        for index, text in enumerate(normalized_texts):
            if term in text:
                matches.append(
                    (_jargon_score(entry.count, index, frequencies.get(term)), index, entry)
                )
                break
    matches.sort(key=lambda match: (-match[0], match[1], -len(match[2].text), match[2].text))
    return _deduplicate(entry for _, _, entry in matches)[:MAX_JARGON_MATCHES]


def _deduplicate(entries: Iterable[LanguageEntry]) -> list[LanguageEntry]:
    selected: dict[tuple[str, str], LanguageEntry] = {}
    for entry in entries:
        key = (normalize(entry.text), normalize(entry.detail))
        previous = selected.get(key)
        if previous is None:
            selected[key] = entry
            continue
        selected[key] = replace(
            previous,
            evidence_ids=tuple(dict.fromkeys((*previous.evidence_ids, *entry.evidence_ids))),
            speakers=previous.speakers | entry.speakers,
            source_refs=tuple(dict.fromkeys((*previous.source_refs, *entry.source_refs))),
        )
    return list(selected.values())


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for run in _TERMS.findall(normalize(text)):
        if run.isascii() or len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def select_expressions(
    entries: Sequence[LanguageEntry],
    topic: str,
    intent: str,
    avoid: tuple[str, ...],
    speaker: str,
) -> list[LanguageEntry]:
    """按话题、回复意图和已观察的说话人选取表达。"""
    topic_terms, intent_terms = _terms(topic), _terms(intent)
    excluded = tuple(filter(None, map(normalize, avoid)))
    ranked: list[tuple[int, int, LanguageEntry]] = []
    for entry in entries:
        style = normalize(entry.text)
        if any(term in style for term in excluded):
            continue
        terms = _terms(f"{entry.detail} {entry.text}")
        topic_hits = len(terms & topic_terms)
        intent_hits = len(terms & intent_terms)
        if topic_hits or intent_hits:
            score = topic_hits * 2 + intent_hits
            ranked.append((score, int(speaker in entry.speakers), entry))
    ranked.sort(key=lambda match: (-match[0], -match[1], -match[2].count, match[2].text))
    return _deduplicate(entry for _, _, entry in ranked)[:MAX_EXPRESSION_MATCHES]


def format_language(entries: Sequence[LanguageEntry], *, jargon: bool, budget: int) -> str:
    """把候选放进内部参考块，超出字数预算的整条跳过。"""
    item = LANGUAGE_JARGON_ITEM if jargon else LANGUAGE_EXPRESSION_ITEM
    lines: list[str] = []
    remaining = budget - len(language_reference_block([], jargon=jargon))
    for entry in entries:
        text = " ".join(entry.text.split())
        detail = " ".join(entry.detail.split())
        line = item.fill(text=text, detail=detail)
        if len(line) + 1 <= remaining:
            lines.append(line)
            remaining -= len(line) + 1
    return language_reference_block(lines, jargon=jargon) if lines else ""
