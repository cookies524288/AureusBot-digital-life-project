"""心跳的增量语言学习任务，材料只来自本次运行的新入站消息（D89）。"""

import asyncio
import json
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace

from aureus.common.language_prompts import LANGUAGE_LEARNER_FORMAT, LANGUAGE_LEARNER_PROMPT
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage
from aureus.heartbeat.perception import Perception, TrustLevel
from aureus.heartbeat.planning import first_json_object
from aureus.memory import (
    MAX_DETAIL,
    MAX_TEXT,
    LanguageKind,
    LanguageProposal,
    LanguageSample,
    LanguageStore,
)
from aureus.organs import CallPurpose, ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError
from aureus.timeline import TimelineRecord

logger = logging.getLogger(__name__)
TASK_NAME = "aureus-language-learning"
_MAX_PROPOSALS = 12
_MAX_SAMPLE_CHARS = 400
_MAX_RESPONSE_CHARS = 6000


@dataclass(frozen=True)
class QueuedLanguageSample:
    """一次入站的样本和仅供分批使用的可信路由；未知路由单独处理。"""

    sample: LanguageSample
    conversation: ConversationKey | tuple[str, str] | None


def queue_language_sample(
    record: TimelineRecord, perception: Perception, inbox_message: InboxMessage | None
) -> QueuedLanguageSample:
    """只使用持久收件箱路由，或经通道确认的本机用户身份。"""
    conversation: ConversationKey | tuple[str, str] | None = None
    if inbox_message is not None and perception.route is not None:
        conversation = inbox_message.conversation
    elif (
        perception.channel in ("web", "terminal")
        and perception.trust is TrustLevel.VERIFIED_USER
        and perception.route is None
    ):
        conversation = (perception.channel, perception.sender_id)
    return QueuedLanguageSample(language_sample(record, perception.source_message_id), conversation)


def take_language_batch(queue: deque[QueuedLanguageSample], limit: int) -> list[LanguageSample]:
    """以最早的入站为准选同会话样本，保留其余样本的先后顺序。"""
    head = queue.popleft()
    samples = [head.sample]
    if head.conversation is None:
        return samples
    skipped: list[QueuedLanguageSample] = []
    while queue and len(samples) < limit:
        queued = queue.popleft()
        if queued.conversation == head.conversation:
            samples.append(queued.sample)
        else:
            skipped.append(queued)
    queue.extendleft(reversed(skipped))
    return samples


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


def _parse_proposals(text: str) -> list[LanguageProposal]:
    if len(text) > _MAX_RESPONSE_CHARS:
        return []
    result = first_json_object(text) or {}
    proposals = result.get("proposals")
    if not isinstance(proposals, list):
        return []
    parsed: list[LanguageProposal] = []
    for proposal in proposals[:_MAX_PROPOSALS]:
        if not isinstance(proposal, dict):
            continue
        kind, phrase, detail, ids = (
            proposal.get("kind"),
            proposal.get("text"),
            proposal.get("detail"),
            proposal.get("ids"),
        )
        if (
            kind in (LanguageKind.EXPRESSION, LanguageKind.JARGON)
            and isinstance(phrase, str)
            and isinstance(detail, str)
            and isinstance(ids, list)
        ):
            parsed.append(
                LanguageProposal(
                    LanguageKind(kind),
                    phrase,
                    detail,
                    tuple(number for number in ids if type(number) is int),
                )
            )
    return parsed


async def learn_language(
    store: LanguageStore,
    organ: str,
    call: Callable[[OrganRequest], Awaitable[OrganResponse]],
    samples: Sequence[LanguageSample],
) -> None:
    """让器官提出候选，再由记忆子系统核验来源及原文并保存。"""
    try:
        fresh = await asyncio.to_thread(store.unseen, samples)
        if not fresh:
            return
        presented = {sample.record_id: sample.content[:_MAX_SAMPLE_CHARS] for sample in fresh}
        try:
            response = await call(build_language_learning_request(organ, tuple(presented.values())))
        except (CallRejectedError, OrganCallError):
            logger.info("语言学习器官本次没有返回提议")
            return
        record_ids = dict(enumerate(presented, start=1))
        proposals = [
            replace(
                proposal,
                record_ids=tuple(
                    record_ids[number]
                    for number in dict.fromkeys(proposal.record_ids)
                    if number in record_ids
                ),
            )
            for proposal in _parse_proposals(response.suggestion)
        ]
        await asyncio.to_thread(store.observe, fresh, proposals, presented_content=presented)
    except sqlite3.Error:
        logger.exception("语言学习存储失败")


def build_language_learning_request(organ: str, samples: Sequence[str]) -> OrganRequest:
    """以规则、逐条资料、输出要求的顺序组装实际请求和预览。"""
    return OrganRequest(
        organ_id=organ,
        prompt=LANGUAGE_LEARNER_FORMAT.fill(),
        context={"task": "language_learning"},
        system_context=LANGUAGE_LEARNER_PROMPT.fill(
            max_text=str(MAX_TEXT),
            max_detail=str(MAX_DETAIL),
            output_format="输出要求见最后一条消息。",
            samples="后续每条样本是含 id（本轮消息编号）和 text（原文片段）的 JSON。",
        ),
        purpose=CallPurpose.OPTIONAL,
        model_preference=ModelPreference.CHEAPEST,
        preceding_messages=tuple(
            ("user", json.dumps({"id": index, "text": content}, ensure_ascii=False))
            for index, content in enumerate(samples, start=1)
        ),
    )
