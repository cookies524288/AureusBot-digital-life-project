"""将 3.2 主库暂存中的语言数据映射到运行记忆库。

表达清洗沿用 MaiBot 1.3.15 的 src/learners/expression_style_utils.py（GPL-3.0）。
来源：https://github.com/Mai-with-u/MaiBot
"""

import json
from collections.abc import Iterator
from pathlib import Path

from aureus.memory import (
    EXPRESSION_TABLE,
    FREQUENCY_TABLE,
    JARGON_TABLE,
    ImportedLanguageRow,
    LanguageSnapshotConflictError,
    LanguageStore,
    LanguageTargetError,
)
from aureus.tools.migrate.maibot_catalog import MigrationError, encode_json
from aureus.tools.migrate.maibot_package import Source
from aureus.tools.migrate.maibot_staging import StagedSource, read_staged_source

_MESSAGE_TABLE = "mai_messages"
_PROMPT_EXAMPLES = frozenset({"我嘞个xxxx", "对对对", "这么强！"})


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _count(value: object) -> int:
    return max(0, int(value)) if isinstance(value, int) else 0


def _inactive(values: dict[str, object]) -> bool:
    return any(values.get(field) == 1 for field in ("disabled", "deleted", "is_deleted"))


def _list(value: object) -> list[object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _reference(platform: object, message_id: object) -> tuple[str, str] | None:
    if isinstance(message_id, int) and not isinstance(message_id, bool):
        message_id = str(message_id)
    platform, message_id = _text(platform), _text(message_id)
    return (platform, message_id) if platform and message_id else None


def _evidence(values: dict[str, object]) -> set[tuple[str, str]]:
    return {
        reference
        for group in _list(values.get("evidence_messages"))
        for item in _list(group)
        if isinstance(item, dict)
        if (reference := _reference(item.get("platform"), item.get("message_id"))) is not None
    }


def _source_key(source: Source, table: str, key: str) -> str:
    return encode_json([source.instance, source.role, table, json.loads(key)]).decode("ascii")


def _resolve_evidence(
    staged: StagedSource, source: Source, requested: set[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    matches: dict[tuple[str, str], str] = {}
    ambiguous: set[tuple[str, str]] = set()
    if requested:
        for key, values in staged.rows(_MESSAGE_TABLE):
            reference = _reference(values.get("platform"), values.get("message_id"))
            if reference is None or reference not in requested or reference in ambiguous:
                continue
            if reference in matches:
                del matches[reference]
                ambiguous.add(reference)
            elif not _inactive(values):
                matches[reference] = _source_key(source, _MESSAGE_TABLE, key)
    return matches


def _expressions(staged: StagedSource, source: Source) -> Iterator[ImportedLanguageRow]:
    for key, values in staged.rows(EXPRESSION_TABLE):
        key = _source_key(source, EXPRESSION_TABLE, key)
        text = _text(values.get("style")).removeprefix("使用").strip()
        situation = _text(values.get("situation"))
        example = next(filter(None, map(_text, _list(values.get("content_list")))), "")
        if _inactive(values):
            yield ImportedLanguageRow(EXPRESSION_TABLE, key, values, "skipped", "inactive")
        elif text in _PROMPT_EXAMPLES:
            yield ImportedLanguageRow(EXPRESSION_TABLE, key, values, "skipped", "prompt_example")
        elif not text or not situation:
            yield ImportedLanguageRow(EXPRESSION_TABLE, key, values, "pending", "missing_example")
        else:
            yield ImportedLanguageRow(
                EXPRESSION_TABLE,
                key,
                values,
                "received",
                "missing_example" if not example else "",
                text=text,
                detail=situation,
                example=example,
                count=_count(values.get("count")),
            )


def _jargons(staged: StagedSource, source: Source) -> Iterator[ImportedLanguageRow]:
    rows = list(staged.rows(JARGON_TABLE))
    resolved = _resolve_evidence(staged, source, set().union(*(_evidence(row) for _, row in rows)))
    for key, values in rows:
        key = _source_key(source, JARGON_TABLE, key)
        text, meaning = _text(values.get("content")), _text(values.get("meaning"))
        if _inactive(values):
            yield ImportedLanguageRow(JARGON_TABLE, key, values, "skipped", "inactive")
        elif values.get("is_jargon") == 0 and (meaning or values.get("last_inference_count")):
            yield ImportedLanguageRow(JARGON_TABLE, key, values, "skipped", "classified_non_jargon")
        elif not text or not meaning or values.get("is_jargon") != 1:
            yield ImportedLanguageRow(
                JARGON_TABLE, key, values, "pending", "missing_word_or_meaning"
            )
        else:
            references = _evidence(values)
            evidence = tuple(
                sorted(resolved[reference] for reference in references if reference in resolved)
            )
            yield ImportedLanguageRow(
                JARGON_TABLE,
                key,
                values,
                "received",
                "evidence_unresolved"
                if len(evidence) < len(references)
                else ("missing_evidence" if not evidence else ""),
                text=text,
                detail=meaning,
                count=_count(values.get("count")),
                evidence=evidence,
                unresolved=len(references) - len(evidence),
            )


def _frequencies(staged: StagedSource, source: Source) -> Iterator[ImportedLanguageRow]:
    for key, values in staged.rows(FREQUENCY_TABLE):
        key = _source_key(source, FREQUENCY_TABLE, key)
        term = _text(values.get("term"))
        if _inactive(values):
            yield ImportedLanguageRow(FREQUENCY_TABLE, key, values, "skipped", "inactive")
        elif not term:
            yield ImportedLanguageRow(FREQUENCY_TABLE, key, values, "pending", "missing_term")
        else:
            yield ImportedLanguageRow(
                FREQUENCY_TABLE,
                key,
                values,
                "received",
                text=term,
                count=_count(values.get("occurrence_count")),
                rank=_count(values.get("rank")),
            )


def publish_language(instance: str, staging: Path, target: Path) -> dict[str, object]:
    """从正式暂存只读发布习得语言，报告逐表计数和待处理原因。"""
    source = Source(instance, "main")
    with read_staged_source(source, staging) as staged:
        rows = (
            *_expressions(staged, source),
            *_jargons(staged, source),
            *_frequencies(staged, source),
        )
        try:
            return LanguageStore(target).publish(
                instance, staged.manifest, staged.source_counts(), rows
            )
        except (LanguageSnapshotConflictError, LanguageTargetError) as error:
            raise MigrationError(str(error)) from None
