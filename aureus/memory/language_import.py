"""语言库所持有的 MaiBot 发布记录与逐表对账（D89）。"""

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import groupby
from sqlite3 import Connection
from typing import Literal

from aureus.memory.language_matching import JargonStatus, LanguageEntry, TermFrequency, normalize

EXPRESSION_TABLE = "expressions"
JARGON_TABLE = "jargons"
FREQUENCY_TABLE = "high_frequency_terms"
LANGUAGE_TABLES = (EXPRESSION_TABLE, JARGON_TABLE, FREQUENCY_TABLE)
_LEGACY_EVIDENCE_REASONS = {
    EXPRESSION_TABLE: frozenset({"missing_example"}),
    JARGON_TABLE: frozenset({"missing_evidence", "evidence_unresolved"}),
}
IMPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS language_imports (
    instance TEXT PRIMARY KEY,
    manifest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS language_import_rows (
    instance TEXT NOT NULL REFERENCES language_imports(instance),
    table_name TEXT NOT NULL,
    source_key TEXT NOT NULL,
    raw_values TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL,
    example TEXT NOT NULL,
    count INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    PRIMARY KEY (instance, table_name, source_key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS language_import_candidates
ON language_import_rows(table_name, status);
CREATE TABLE IF NOT EXISTS language_import_observations (
    instance TEXT NOT NULL,
    table_name TEXT NOT NULL,
    source_key TEXT NOT NULL,
    observation_key TEXT NOT NULL REFERENCES language_observations(source_key),
    FOREIGN KEY(instance, table_name, source_key)
        REFERENCES language_import_rows(instance, table_name, source_key),
    PRIMARY KEY(instance, table_name, source_key, observation_key)
) WITHOUT ROWID;
"""


class LanguageSnapshotConflictError(ValueError):
    """该实例已发布过另一份主库快照。"""


@dataclass(frozen=True)
class ImportedLanguageRow:
    """原始行、可查询的字段和已解析的来源证据。"""

    table: str
    key: str
    values: dict[str, object]
    status: Literal["received", "pending", "skipped"]
    reason: str = ""
    text: str = ""
    detail: str = ""
    example: str = ""
    count: int = 0
    rank: int = 0
    evidence: tuple[str, ...] = ()
    unresolved: int = 0


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _promote_legacy_row(connection: Connection, instance: str, row: ImportedLanguageRow) -> bool:
    if row.reason not in _LEGACY_EVIDENCE_REASONS.get(row.table, ()):
        return False
    # 旧表达未写入可召回字段。旧黑话已有这些字段，应保留目标库中的值。
    expression = row.table == EXPRESSION_TABLE
    return (
        connection.execute(
            """UPDATE language_import_rows
               SET status = 'received',
                   text = CASE WHEN ? AND text = '' THEN ? ELSE text END,
                   detail = CASE WHEN ? AND detail = '' THEN ? ELSE detail END,
                   count = CASE WHEN ? AND count = 0 THEN ? ELSE count END
               WHERE instance = ? AND table_name = ? AND source_key = ?
                 AND status = 'pending' AND reason = ?""",
            (
                expression,
                row.text,
                expression,
                row.detail,
                expression,
                row.count,
                instance,
                row.table,
                row.key,
                row.reason,
            ),
        ).rowcount
        == 1
    )


def publish_rows(
    connection: Connection,
    instance: str,
    manifest: str,
    source_counts: dict[str, int],
    rows: Iterable[ImportedLanguageRow],
) -> dict[str, object]:
    """事务内发布一份快照，并按 D92 提升已有快照中仅缺证据的行。"""
    previous = connection.execute(
        "SELECT manifest FROM language_imports WHERE instance = ?", (instance,)
    ).fetchone()
    if previous is not None and previous["manifest"] != manifest:
        raise LanguageSnapshotConflictError("language_snapshot_conflict")
    if previous is None:
        connection.execute("INSERT INTO language_imports VALUES (?, ?)", (instance, manifest))

    counts = {table: Counter[str]() for table in LANGUAGE_TABLES}
    reasons = {table: Counter[str]() for table in LANGUAGE_TABLES}
    for row in rows:
        status = row.status
        if previous is None:
            connection.execute(
                """INSERT INTO language_import_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    instance,
                    row.table,
                    row.key,
                    _encode(row.values),
                    row.status,
                    row.reason,
                    row.text,
                    row.detail,
                    row.example,
                    row.count,
                    row.rank,
                    _encode(row.evidence),
                ),
            )
        elif row.status == "received" and not _promote_legacy_row(connection, instance, row):
            status = "already_exists"
        counts[row.table][status] += 1
        counts[row.table]["unresolved_evidence"] += row.unresolved
        if row.reason:
            reasons[row.table][row.reason] += 1
    tables: dict[str, object] = {}
    for table, count in counts.items():
        source = source_counts.get(table, 0)
        missing = source - sum(
            count[key] for key in ("received", "already_exists", "pending", "skipped")
        )
        if missing < 0:
            raise ValueError("language_staging_count_mismatch")
        if missing:
            reasons[table]["stage_not_exported"] += missing
        tables[table] = {
            "source": source,
            "received": count["received"],
            "already_exists": count["already_exists"],
            "pending": count["pending"] + missing,
            "skipped": count["skipped"],
            "unresolved_evidence": count["unresolved_evidence"],
            "reasons": dict(reasons[table]),
        }
    return {"operation": "publish-language", "role": "main", "tables": tables}


def imported_entries(connection: Connection, table: str) -> list[LanguageEntry]:
    """读取导入语言和黑话候选，来源引用与本地时间线编号分别保留。"""
    references: list[LanguageEntry] = []
    rows = connection.execute(
        """SELECT entry.instance, entry.source_key, entry.text, entry.detail, entry.example,
                  entry.status, entry.reason, entry.raw_values,
                  entry.count, entry.evidence, sample.record_id, sample.speaker
           FROM language_import_rows AS entry
           LEFT JOIN language_import_observations AS observed
               USING (instance, table_name, source_key)
           LEFT JOIN language_observations AS sample ON sample.source_key = observed.observation_key
           WHERE entry.table_name = ? AND (entry.status = 'received' OR
                 (entry.table_name = 'jargons' AND entry.reason IN
                   ('classified_non_jargon', 'missing_word_or_meaning')))
           ORDER BY entry.instance, entry.source_key, sample.record_id""",
        (table,),
    )
    for source, group in groupby(rows, key=lambda row: (row["instance"], row["source_key"])):
        records = list(group)
        first = records[0]
        evidence = records if first["record_id"] is not None else []
        status = None
        text, detail = first["text"], first["detail"]
        if table == JARGON_TABLE:
            raw = json.loads(first["raw_values"])
            text = text or str(raw.get("content") or "").strip()
            detail = detail or str(raw.get("meaning") or "").strip()
            if not text:
                continue
            if first["reason"] == "classified_non_jargon":
                status = JargonStatus.NON_JARGON
            elif first["status"] == "received" or raw.get("is_jargon") == 1:
                status = JargonStatus.CONFIRMED
            else:
                status = JargonStatus.CANDIDATE
        references.append(
            LanguageEntry(
                text,
                detail,
                first["example"],
                first["count"] + len(evidence),
                tuple(row["record_id"] for row in evidence),
                frozenset(row["speaker"] for row in evidence),
                source_refs=(first["source_key"], *json.loads(first["evidence"])),
                entry_key=_encode((table, *source)),
                jargon_status=status,
            )
        )
    return references


def imported_frequencies(connection: Connection) -> dict[str, TermFrequency]:
    """合计词频，取最靠前的正排名。"""
    result: dict[str, TermFrequency] = {}
    for row in connection.execute(
        """SELECT text, rank, count FROM language_import_rows
           WHERE table_name = ? AND status = 'received'""",
        (FREQUENCY_TABLE,),
    ):
        key, rank = normalize(row["text"]), row["rank"]
        existing = result.get(key, TermFrequency(0, 0))
        ranks = [number for number in (rank, existing.rank) if number > 0]
        result[key] = TermFrequency(min(ranks, default=0), existing.count + row["count"])
    return result
