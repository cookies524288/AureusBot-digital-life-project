"""心跳的增量语言学习任务，材料只来自本次运行的新入站消息（D89）。"""

import asyncio
import json
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from aureus.heartbeat.inbox_store import InboxMessage
from aureus.heartbeat.language_requests import (
    build_comparison,
    build_content_only,
    build_extraction,
    build_with_context,
)
from aureus.heartbeat.perception import Perception, TrustLevel
from aureus.memory import (
    MAX_DETAIL,
    JargonInference,
    LanguageKind,
    LanguageProposal,
    LanguageSample,
    LanguageStore,
)
from aureus.organs import OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError
from aureus.timeline import TimelineRecord

logger = logging.getLogger(__name__)
TASK_NAME = "aureus-language-learning"
_MAX_PROPOSALS = {LanguageKind.EXPRESSION: 10, LanguageKind.JARGON: 30}
_MAX_SAMPLE_CHARS = 400
_MAX_RESPONSE_CHARS = 6000
OrganCall = Callable[[OrganRequest], Awaitable[OrganResponse]]


@dataclass(frozen=True)
class QueuedLanguageSample:
    """一次入站的样本和仅供分批使用的可信路由；未知路由单独处理。"""

    sample: LanguageSample
    conversation: str | None


def queue_language_sample(
    record: TimelineRecord, perception: Perception, inbox_message: InboxMessage | None
) -> QueuedLanguageSample:
    """以持久收件箱路由或可信本机用户分开组织学习证据。"""
    conversation = None
    if inbox_message is not None:
        conversation = inbox_message.conversation.language_scope_key()
    elif (
        perception.channel in ("web", "terminal")
        and perception.trust is TrustLevel.VERIFIED_USER
        and perception.route is None
    ):
        conversation = json.dumps((perception.channel, perception.sender_id), separators=(",", ":"))
    return QueuedLanguageSample(language_sample(record, perception.source_message_id), conversation)


def take_language_batch(
    queue: deque[QueuedLanguageSample], limit: int
) -> tuple[str | None, list[LanguageSample]]:
    """以最早的入站为准选同会话样本，保留其余样本的先后顺序。"""
    head = queue.popleft()
    samples = [head.sample]
    if head.conversation is None:
        return None, samples
    skipped: list[QueuedLanguageSample] = []
    while queue and len(samples) < limit:
        queued = queue.popleft()
        if queued.conversation == head.conversation:
            samples.append(queued.sample)
        else:
            skipped.append(queued)
    queue.extendleft(reversed(skipped))
    return head.conversation, samples


def speaker_key(record: TimelineRecord) -> str:
    """优先按已识别人物跨通道关联，未知时按通道和发言账号关联。"""
    person_id = record.metadata.get("person_id")
    if type(person_id) is int:
        return f"person:{person_id}"
    return json.dumps((record.metadata.get("channel"), record.actor_id), ensure_ascii=False)


def language_sample(record: TimelineRecord, source_message_id: str | None) -> LanguageSample:
    """保存可回查的首次入站记录；通道重投时沿用稳定来源键。"""
    key = (
        json.dumps(
            (record.metadata.get("channel"), record.actor_id, source_message_id), ensure_ascii=False
        )
        if source_message_id
        else f"timeline:{record.id}"
    )
    return LanguageSample(key, record.id, speaker_key(record), record.content)


def _decode(text: str) -> Any:
    if len(text) > _MAX_RESPONSE_CHARS:
        return None
    text = text.strip()
    if text.startswith(("```json\n", "```\n")) and text.endswith("```"):
        text = text.split("\n", 1)[1][:-3]
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _parse_proposals(
    proposals: list[object], kind: LanguageKind, records: dict[str, int]
) -> list[LanguageProposal]:
    parsed: list[LanguageProposal] = []
    for proposal in proposals[: _MAX_PROPOSALS[kind]]:
        if not isinstance(proposal, dict):
            continue
        number = proposal.get("id")
        if type(number) is int:
            number = str(number)
        if not isinstance(number, str) or number not in records:
            continue
        if kind == LanguageKind.EXPRESSION:
            phrase, detail = proposal.get("style"), proposal.get("situation")
            if isinstance(phrase, str):
                phrase = phrase.removeprefix("使用 ").removeprefix("使用：")
        else:
            phrase, detail = proposal.get("content"), ""
        if isinstance(phrase, str) and isinstance(detail, str):
            parsed.append(LanguageProposal(kind, phrase, detail, (records[number],)))
    return parsed


async def learn_language(
    store: LanguageStore,
    organ: str,
    call: OrganCall,
    samples: Sequence[LanguageSample],
    *,
    conversation_key: str | None = None,
) -> None:
    """独立抽取表达和候选黑话，并只对同会话的有效来源推断。"""
    try:
        await asyncio.to_thread(store.observe, samples, ())
        if conversation_key is None:
            return
        for kind in LanguageKind:
            attempts = await _extract(store, organ, call, samples, conversation_key, kind)
            for attempt in attempts:
                await _infer(store, organ, call, attempt)
    except sqlite3.Error:
        logger.exception("语言学习存储失败")


async def _extract(
    store: LanguageStore,
    organ: str,
    call: OrganCall,
    samples: Sequence[LanguageSample],
    scope: str,
    kind: LanguageKind,
) -> list[JargonInference]:
    fresh = await asyncio.to_thread(store.pending_extraction, kind, samples)
    if not fresh:
        return []
    presented = {sample.record_id: sample.content[:_MAX_SAMPLE_CHARS] for sample in fresh}
    result = await _ask(call, build_extraction(organ, kind, tuple(presented.values())))
    if not isinstance(result, list):
        return []
    records = {str(number): record for number, record in enumerate(presented, start=1)}
    return await asyncio.to_thread(
        store.observe_extraction,
        kind,
        scope,
        fresh,
        presented,
        _parse_proposals(result, kind, records),
    )


async def _ask(call: OrganCall, request: OrganRequest) -> Any:
    try:
        return _decode((await call(request)).suggestion)
    except (CallRejectedError, OrganCallError):
        logger.info("语言学习器官本次没有返回提议：%s", request.context["task"])
        return None


def _meaning(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    value = result.get("meaning")
    if not isinstance(value, str) or len(value.strip()) > MAX_DETAIL:
        return None
    return value.strip()


async def _infer(
    store: LanguageStore, organ: str, call: OrganCall, attempt: JargonInference
) -> None:
    contextual = await _ask(
        call, build_with_context(organ, attempt.text, attempt.evidence, attempt.previous_meaning)
    )
    if not isinstance(contextual, dict) or type(contextual.get("no_info")) is not bool:
        return
    meaning = _meaning(contextual)
    if contextual["no_info"] or meaning == "":
        await asyncio.to_thread(store.record_jargon_inference, attempt, "insufficient")
        return
    if meaning is None:
        return
    standalone = _meaning(await _ask(call, build_content_only(organ, attempt.text)))
    if standalone is None:
        return
    comparison = await _ask(call, build_comparison(organ, meaning, standalone))
    if not isinstance(comparison, dict) or type(comparison.get("is_similar")) is not bool:
        return
    if comparison["is_similar"]:
        await asyncio.to_thread(store.record_jargon_inference, attempt, "similar")
    else:
        await asyncio.to_thread(store.record_jargon_inference, attempt, "accepted", meaning)
