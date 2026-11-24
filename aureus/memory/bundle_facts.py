"""把包内事实账本和外部引用投影成真实记忆原件及证据。"""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from aureus.memory.bundle_format import MemoryBundleError
from aureus.memory.history import HistoricalMemory, MemoryEvidence, MemorySource, MemoryStatus

_STATES: dict[str, MemoryStatus] = {
    "active": "active",
    "conflicted": "conflicted",
    "superseded": "superseded",
    "retracted": "retracted",
}
_TARGETS = {
    "paragraph": "paragraphs",
    "relation": "relations",
    "entity": "entities",
    "episode": "episodes",
    "fact": "fact_claims",
}


def project_facts(
    tables: dict[str, list[dict[str, Any]]],
    scope: str,
    records: dict[MemorySource, HistoricalMemory],
    stamp: Callable[[object], datetime | None],
) -> None:
    """保留声明的独立有效期、证据立场、转换及外部标识，不猜测缺失目标。"""

    def source(table: str, *keys: str | int) -> MemorySource:
        return MemorySource("a-memorix-bundle", scope, table, tuple(str(key) for key in keys))

    def evidence(kind: str, key: str, label: str = "") -> MemoryEvidence:
        table = _TARGETS.get(kind)
        target = records.get(source(table, key)) if table else None
        return MemoryEvidence(label or kind, key, target.source if target else None)

    def append(owner: MemorySource, *links: MemoryEvidence) -> None:
        record = records[owner]
        records[owner] = replace(record, evidence=record.evidence + links)

    for row in tables["fact_claims"]:
        key, value = row.get("fact_key"), row.get("value_text")
        if not isinstance(key, str) or not isinstance(value, str):
            raise MemoryBundleError("事实声明缺少 fact_key 或 value_text。")
        item = source("fact_claims", row["claim_id"])
        polarity = "否定：" if row.get("polarity") == "negative" else ""
        state = str(row.get("status") or "")
        if state not in _STATES:
            raise MemoryBundleError("事实声明的状态无法恢复。")
        labels = tuple(
            MemoryEvidence(kind, label)
            for kind, label in (
                ("maibot_scope_type", row.get("scope_type")),
                ("maibot_scope_id", row.get("scope_id")),
            )
            if isinstance(label, str) and label
        )
        records[item] = HistoricalMemory(
            item,
            f"{polarity}{key}：{value}",
            recorded_at=stamp(row.get("first_observed_at")),
            status=_STATES[state],
            valid_from=stamp(row.get("valid_from")),
            valid_to=stamp(row.get("valid_to")),
            evidence=labels,
        )
    for row in tables["fact_evidence"]:
        claim_id, kind, key, stance = (
            str(row[field])
            for field in (
                "claim_id",
                "evidence_type",
                "evidence_id",
                "stance",
            )
        )
        claim = source("fact_claims", claim_id)
        if claim not in records:
            raise MemoryBundleError("事实证据缺少所属声明。")
        item = source("fact_evidence", claim_id, kind, key, stance)
        link = evidence(kind, key)
        records[item] = HistoricalMemory(
            item, "", status="missing", evidence=(link,), recorded_at=stamp(row.get("observed_at"))
        )
        append(claim, replace(link, kind=f"{stance}:{kind}"))
    for row in tables["fact_transitions"]:
        item = source("fact_transitions", row["transition_id"])
        claims = tuple(
            evidence("fact", str(row[key]), key)
            for key in (
                "old_claim_id",
                "new_claim_id",
            )
            if row.get(key)
        )
        basis = (
            (evidence(str(row["evidence_type"]), str(row["evidence_id"])),)
            if (row.get("evidence_type") and row.get("evidence_id"))
            else ()
        )
        records[item] = HistoricalMemory(
            item,
            str(row.get("transition_type") or ""),
            status="missing",
            evidence=claims + basis,
            recorded_at=stamp(row.get("created_at")),
        )
        for link in claims:
            if link.source:
                append(link.source, MemoryEvidence(link.kind, str(row["transition_id"]), item))
    for row in tables["external_memory_refs"]:
        target = source("paragraphs", str(row.get("paragraph_hash")))
        if target not in records:
            raise MemoryBundleError("外部引用缺少关联段落。")
        external_id = str(row["external_id"])
        item = source("external_memory_refs", external_id)
        records[item] = HistoricalMemory(
            item,
            external_id,
            status="missing",
            evidence=(MemoryEvidence("paragraph", str(target.key[0]), target),),
            recorded_at=stamp(row.get("created_at")),
        )
        append(target, MemoryEvidence("external_reference", external_id, item))
