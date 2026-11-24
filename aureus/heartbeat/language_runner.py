"""心跳的增量语言学习任务，材料只来自本次运行的新入站消息（D89）。

表达过滤改编自 MaiBot 1.3.15（GPL-3.0）的 _filter_expressions 和风格清洗；
沿用本项目的来源键及展示范围。
https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/learners/expression_learner.py#L639
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from aureus.common import Clock
from aureus.heartbeat.events import EVENT_MESSAGE
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage
from aureus.heartbeat.language_requests import (
    build_comparison,
    build_content_only,
    build_expression_quality,
    build_extraction,
    build_with_context,
)
from aureus.heartbeat.pending_learning_buffer import PendingLearningBuffer
from aureus.heartbeat.perception import Perception, TrustLevel
from aureus.memory import (
    MAX_DETAIL,
    ExpressionQualityDecision,
    ExpressionQualityReason,
    ExpressionQualityStatus,
    JargonInference,
    LanguageKind,
    LanguageProposal,
    LanguageSample,
    LanguageStore,
    expression_proposals,
)
from aureus.organs import OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError
from aureus.timeline import ActorType, TimelineDatabase, TimelineRecord

logger = logging.getLogger(__name__)
TASK_NAME = "aureus-language-learning"
_MAX_PROPOSALS = {LanguageKind.EXPRESSION: 10, LanguageKind.JARGON: 30}
_MAX_SAMPLE_CHARS = 400
_MAX_RESPONSE_CHARS = 6000
OrganCall = Callable[[OrganRequest], Awaitable[OrganResponse]]
_QUALITY_REASONS = {
    (True, "matched"): (ExpressionQualityStatus.PASSED, ExpressionQualityReason.MATCHED),
    (False, "mismatch"): (ExpressionQualityStatus.REJECTED, ExpressionQualityReason.MISMATCH),
    (False, "too_specific"): (
        ExpressionQualityStatus.REJECTED,
        ExpressionQualityReason.TOO_SPECIFIC,
    ),
}


@dataclass(frozen=True)
class ExpressionQuality:
    """显式启用的质检及其独立器官；未绑定时只记录错误，不回退。"""

    organ: str | None
    clock: Clock

    async def assess(
        self, call: OrganCall, proposal: LanguageProposal
    ) -> ExpressionQualityDecision:
        """获取一条有界结构化建议，按注入时钟记录，不捕获任务取消。"""
        status, reason = await self._assess(call, proposal)
        return ExpressionQualityDecision(status, reason, self.clock.now().isoformat())

    async def _assess(
        self, call: OrganCall, proposal: LanguageProposal
    ) -> tuple[ExpressionQualityStatus, ExpressionQualityReason]:
        if not self.organ:
            return ExpressionQualityStatus.ERROR, ExpressionQualityReason.NO_ROUTE
        try:
            reply = await call(build_expression_quality(self.organ, proposal.detail, proposal.text))
        except CallRejectedError:
            return ExpressionQualityStatus.ERROR, ExpressionQualityReason.BLOCKED
        except ValueError:
            return ExpressionQualityStatus.ERROR, ExpressionQualityReason.NO_ROUTE
        except Exception as error:
            logger.warning("表达质检器官失败：%s", type(error).__name__)
            return ExpressionQualityStatus.ERROR, ExpressionQualityReason.FAILED
        result = _decode(reply.suggestion)
        if (
            isinstance(result, dict)
            and result.keys() == {"suitable", "reason"}
            and type(result.get("suitable")) is bool
            and isinstance(result.get("reason"), str)
        ):
            return _QUALITY_REASONS.get(
                (result["suitable"], result["reason"]),
                (ExpressionQualityStatus.ERROR, ExpressionQualityReason.INVALID),
            )
        return ExpressionQualityStatus.ERROR, ExpressionQualityReason.INVALID


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
    else:
        conversation = local_language_scope(perception)
    return QueuedLanguageSample(
        language_sample(record, perception.source_message_id, conversation), conversation
    )


def local_language_scope(perception: Perception) -> str | None:
    """识别不使用平台收件箱的可信本机私聊。"""
    if (
        perception.channel in ("web", "terminal")
        and perception.trust is TrustLevel.VERIFIED_USER
        and perception.route is None
    ):
        return json.dumps((perception.channel, perception.sender_id), separators=(",", ":"))
    return None


def take_language_batch(
    queue: PendingLearningBuffer[QueuedLanguageSample], limit: int
) -> tuple[str | None, list[LanguageSample]]:
    """以最早的入站为准选同会话样本，保留其余样本的先后顺序。"""
    pending = queue.snapshot()
    scope = pending[0].conversation
    selected = (
        [item for item in pending if item.conversation == scope][:limit]
        if scope is not None
        else pending[:1]
    )
    queue.discard(selected)
    return scope, [item.sample for item in selected]


def speaker_key(record: TimelineRecord) -> str:
    """优先按已识别人物跨通道关联，未知时按通道和发言账号关联。"""
    person_id = record.metadata.get("person_id")
    if type(person_id) is int:
        return f"person:{person_id}"
    return json.dumps((record.metadata.get("channel"), record.actor_id), ensure_ascii=False)


def language_sample(
    record: TimelineRecord, source_message_id: str | None, conversation: str | None = None
) -> LanguageSample:
    """保存可回查的首次入站记录；通道重投时沿用稳定来源键。"""
    if not source_message_id:
        return LanguageSample(
            f"timeline:{record.id}", record.id, speaker_key(record), record.content
        )
    parts = (record.metadata.get("channel"), record.actor_id, source_message_id)
    key = json.dumps((conversation, *parts), ensure_ascii=False)
    previous = json.dumps(parts, ensure_ascii=False)
    return LanguageSample(
        key, record.id, speaker_key(record), record.content, previous, conversation
    )


def previous_language_scope(
    timeline: TimelineDatabase, record_id: int, previous_key: str
) -> str | None:
    """从旧观察的不可变时间线原件核对平台地址，不用当前重投地址猜。"""
    record = timeline.get(record_id)
    if record is None or record.event_type != EVENT_MESSAGE:
        return None
    if record.actor_type not in (ActorType.USER, ActorType.PERSON):
        return None
    metadata = record.metadata
    source_message = metadata.get("source_message_id")
    if not isinstance(source_message, str) or not source_message:
        return None
    if language_sample(record, source_message).previous_source_key != previous_key:
        return None
    channel, trust = metadata.get("channel"), metadata.get("trust")
    if channel in ("web", "terminal") and trust == TrustLevel.VERIFIED_USER:
        return json.dumps((channel, record.actor_id), separators=(",", ":"))
    if channel != "qq" or type(metadata.get("inbox_message_id")) is not int:
        return None
    account, kind, recipient, temporary_group = (
        metadata.get("account_id"),
        metadata.get("message_type"),
        metadata.get("recipient_id"),
        metadata.get("temporary_group_id"),
    )
    if (
        not isinstance(account, str)
        or not account
        or not isinstance(recipient, str)
        or not recipient
        or kind not in ("group", "private")
        or (temporary_group is not None and not isinstance(temporary_group, str))
    ):
        return None
    return ConversationKey(channel, account, kind, recipient, temporary_group).language_scope_key()


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
    proposals: list[object],
    kind: LanguageKind,
    records: dict[str, LanguageSample],
    banned_names: set[str],
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
        sample = records[number]
        if kind == LanguageKind.EXPRESSION:
            phrase, detail = proposal.get("style"), proposal.get("situation")
            if not isinstance(phrase, str) or "SELF" in sample.content:
                continue
            phrase = phrase.strip()
            prefix = "使用：" if phrase.startswith("使用：") else "使用"
            phrase = phrase.removeprefix(prefix).strip()
            if not phrase or phrase.casefold() in banned_names:
                continue
        else:
            phrase, detail = proposal.get("content"), ""
        if isinstance(phrase, str) and isinstance(detail, str):
            parsed.append(LanguageProposal(kind, phrase, detail, (sample.record_id,)))
    return parsed


async def learn_language(
    store: LanguageStore,
    organ: str,
    call: OrganCall,
    samples: Sequence[LanguageSample],
    *,
    conversation_key: str | None = None,
    bot_names: Sequence[str] = (),
    expression_quality: ExpressionQuality | None = None,
) -> None:
    """独立抽取表达和候选黑话，并只对同会话的有效来源推断。"""
    try:
        await asyncio.to_thread(store.observe, samples, ())
        if conversation_key is None:
            return
        banned_names = {name.strip().casefold() for name in bot_names if name.strip()}
        for kind in LanguageKind:
            attempts = await _extract(
                store,
                organ,
                call,
                samples,
                conversation_key,
                kind,
                banned_names,
                expression_quality if kind == LanguageKind.EXPRESSION else None,
            )
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
    banned_names: set[str],
    expression_quality: ExpressionQuality | None,
) -> list[JargonInference]:
    fresh = await asyncio.to_thread(store.pending_extraction, kind, samples)
    if not fresh:
        return []
    evidence = {sample.record_id: sample for sample in fresh}
    presented = {
        record_id: sample.content[:_MAX_SAMPLE_CHARS] for record_id, sample in evidence.items()
    }
    result = await _ask(call, build_extraction(organ, kind, tuple(presented.values())))
    if not isinstance(result, list):
        return []
    records = {str(number): sample for number, sample in enumerate(evidence.values(), start=1)}
    proposals = _parse_proposals(result, kind, records, banned_names)
    quality = (
        {
            candidate: await expression_quality.assess(call, candidate)
            for candidate in expression_proposals(proposals, presented)
        }
        if expression_quality is not None
        else None
    )
    return await asyncio.to_thread(
        store.observe_extraction,
        kind,
        scope,
        fresh,
        presented,
        proposals,
        quality=quality,
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
