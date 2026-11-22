"""心跳的增量语言学习任务，材料只来自本次运行的新入站消息（D89）。"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence

from aureus.common.language_prompts import LANGUAGE_LEARNER_PROMPT
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
        data = {sample.record_id: sample.content[:_MAX_SAMPLE_CHARS] for sample in fresh}
        prompt = LANGUAGE_LEARNER_PROMPT.format(
            max_text=MAX_TEXT, max_detail=MAX_DETAIL, samples=json.dumps(data, ensure_ascii=False)
        )
        try:
            response = await call(
                OrganRequest(
                    organ_id=organ,
                    prompt=prompt,
                    context={"task": "language_learning", "source_ids": list(data)},
                    purpose=CallPurpose.OPTIONAL,
                    model_preference=ModelPreference.CHEAPEST,
                )
            )
        except (CallRejectedError, OrganCallError):
            logger.info("语言学习器官本次没有返回提议")
            return
        await asyncio.to_thread(store.observe, fresh, _parse_proposals(response.suggestion))
    except sqlite3.Error:
        logger.exception("语言学习存储失败")
