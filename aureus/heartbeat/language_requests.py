"""心跳语言学习的五个实际请求及其共用预览构建器。"""

import json
from collections.abc import Sequence

from aureus.common.language_prompts import (
    JARGON_COMPARE,
    JARGON_CONTENT_ONLY,
    JARGON_WITH_CONTEXT,
    LANGUAGE_LEARNER_FORMAT,
    LANGUAGE_LEARNER_PROMPT,
    LEARN_JARGON,
    LEARN_STYLE,
)
from aureus.memory import MAX_DETAIL, MAX_TEXT, LanguageKind
from aureus.organs import CallPurpose, ModelPreference, OrganRequest


def _numbered(samples: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        json.dumps({"id": index, "text": content}, ensure_ascii=False)
        for index, content in enumerate(samples, start=1)
    )


def _request(
    organ: str, task: str, prompt: str, system: str, sources: Sequence[str]
) -> OrganRequest:
    return OrganRequest(
        organ_id=organ,
        prompt=prompt,
        context={"task": task},
        system_context=system,
        purpose=CallPurpose.OPTIONAL,
        model_preference=ModelPreference.CHEAPEST,
        preceding_messages=tuple(("user", content) for content in sources),
    )


def build_extraction(organ: str, kind: LanguageKind, samples: Sequence[str]) -> OrganRequest:
    """将每条来源作为单独 user 消息，最后一条指定此路的输出格式。"""
    template = LEARN_STYLE if kind == LanguageKind.EXPRESSION else LEARN_JARGON
    system = LANGUAGE_LEARNER_PROMPT.fill(
        max_text=str(MAX_TEXT),
        max_detail=str(MAX_DETAIL),
        output_format="输出要求见最后一条消息。",
        samples="后续每条样本是含 id（本轮消息编号）和 text（原文片段）的 JSON。",
    )
    return _request(organ, template.key, template.fill(), system, _numbered(samples))


def build_with_context(
    organ: str, text: str, evidence: Sequence[str], previous_meaning: str = ""
) -> OrganRequest:
    """提供真实会话证据，只以同词的先前确认释义作参考。"""
    prompt = JARGON_WITH_CONTEXT.fill(
        content=json.dumps(text, ensure_ascii=False),
        previous_meaning=(
            "先前释义仅供参考：" + json.dumps(previous_meaning, ensure_ascii=False)
            if previous_meaning
            else ""
        ),
    )
    return _request(
        organ, JARGON_WITH_CONTEXT.key, prompt, LANGUAGE_LEARNER_FORMAT.fill(), _numbered(evidence)
    )


def build_content_only(organ: str, text: str) -> OrganRequest:
    """不给证据、先前释义或会话标识，独立解释词条。"""
    prompt = JARGON_CONTENT_ONLY.fill(content=json.dumps(text, ensure_ascii=False))
    return _request(organ, JARGON_CONTENT_ONLY.key, prompt, LANGUAGE_LEARNER_FORMAT.fill(), ())


def build_comparison(organ: str, contextual: str, standalone: str) -> OrganRequest:
    """比较两个已解析的释义；不重复提供会话证据。"""
    data = (
        json.dumps({"with_context": contextual}, ensure_ascii=False),
        json.dumps({"content_only": standalone}, ensure_ascii=False),
    )
    return _request(
        organ, JARGON_COMPARE.key, JARGON_COMPARE.fill(), LANGUAGE_LEARNER_FORMAT.fill(), data
    )
