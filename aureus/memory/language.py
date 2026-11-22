"""记忆子系统的习得语言和原始消息证据（D89）。"""

from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby
from pathlib import Path
from sqlite3 import Connection, Row, connect

from aureus.common.sqlite import open_connection
from aureus.memory.language_import import (
    EXPRESSION_TABLE,
    IMPORT_SCHEMA,
    JARGON_TABLE,
    ImportedLanguageRow,
    imported_entries,
    imported_frequencies,
    publish_rows,
)
from aureus.memory.language_matching import (
    MIN_JARGON_EVIDENCE,
    LanguageEntry,
    format_language,
    is_reference,
    match_jargons,
    normalize,
    select_expressions,
)

MAX_TEXT = 80
MAX_DETAIL = 160
BATCH_SIZE = 16
_EXCLUDED_MARKERS = ("SELF", "[表情包", "[图片")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS language_observations (
    source_key TEXT PRIMARY KEY,
    record_id INTEGER NOT NULL,
    speaker TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS language_entries (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL,
    example TEXT NOT NULL,
    text_key TEXT NOT NULL,
    detail_key TEXT NOT NULL,
    UNIQUE(kind, text_key, detail_key)
);
CREATE TABLE IF NOT EXISTS language_evidence (
    entry_id INTEGER NOT NULL REFERENCES language_entries(id),
    source_key TEXT NOT NULL REFERENCES language_observations(source_key),
    PRIMARY KEY(entry_id, source_key)
);
"""


class LanguageKind(StrEnum):
    """两种语言材料的用途。"""

    EXPRESSION = "expression"
    JARGON = "jargon"


class LanguageTargetError(ValueError):
    """语言发布要求一个已有的 Aureus 运行主库。"""


@dataclass(frozen=True)
class LanguageSample:
    """来自正常入站消息的样本，时间、原文和可见范围通过时间线序号定位。"""

    source_key: str
    record_id: int
    speaker: str
    content: str


@dataclass(frozen=True)
class LanguageProposal:
    """器官建议，证据仍须由记忆子系统核验。"""

    kind: LanguageKind
    text: str
    detail: str
    record_ids: tuple[int, ...]


def _is_learnable(sample: LanguageSample) -> bool:
    return (
        bool(normalize(sample.content))
        and not is_reference(sample.content)
        and not any(marker in sample.content for marker in _EXCLUDED_MARKERS)
    )


def _supporting_samples(
    proposal: LanguageProposal, samples: dict[int, LanguageSample]
) -> list[LanguageSample]:
    term, detail = normalize(proposal.text), normalize(proposal.detail)
    if not term or not detail or len(term) > MAX_TEXT or len(detail) > MAX_DETAIL:
        return []
    if term == detail or any(
        marker in f"{proposal.text} {proposal.detail}" for marker in _EXCLUDED_MARKERS
    ):
        return []
    if is_reference(proposal.text) or is_reference(proposal.detail):
        return []
    return [
        samples[record_id]
        for record_id in dict.fromkeys(proposal.record_ids)
        if record_id in samples and term in normalize(samples[record_id].content)
    ]


class LanguageStore:
    """在同一个运行主库读写习得语言，连接只在当前操作内使用。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def open(self) -> None:
        """初始化语言子系统持有的表。"""
        with closing(open_connection(self._path)) as connection:
            connection.executescript(_SCHEMA + IMPORT_SCHEMA)

    def publish(
        self,
        instance: str,
        manifest: str,
        source_counts: dict[str, int],
        rows: Iterable[ImportedLanguageRow],
    ) -> dict[str, object]:
        """发布一份正式暂存快照到显式指定的 Aureus 运行主库。"""
        uri = f"{self._path.absolute().as_uri()}?mode=rw"
        with closing(connect(uri, uri=True)) as connection, connection:
            connection.row_factory = Row
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'timeline'"
                ).fetchone()
                is None
            ):
                raise LanguageTargetError("language_target_not_runtime")
            connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + IMPORT_SCHEMA)
            return publish_rows(connection, instance, manifest, source_counts, rows)

    def unseen(self, samples: Sequence[LanguageSample]) -> list[LanguageSample]:
        """取本批还没有消费的真实样本，同一来源取首条。"""
        unique: dict[str, LanguageSample] = {}
        for sample in samples:
            if _is_learnable(sample):
                unique.setdefault(sample.source_key, sample)
        with closing(open_connection(self._path)) as connection:
            return [
                sample
                for key, sample in unique.items()
                if connection.execute(
                    "SELECT 1 FROM language_observations WHERE source_key = ?", (key,)
                ).fetchone()
                is None
            ]

    def observe(
        self, samples: Sequence[LanguageSample], proposals: Sequence[LanguageProposal]
    ) -> None:
        """一次事务核验真实来源、记录观察，并保存证据支持的新语言提议。"""
        with closing(open_connection(self._path)) as connection, connection:
            accepted: dict[int, LanguageSample] = {}
            for sample in samples:
                if not _is_learnable(sample):
                    continue
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO language_observations VALUES (?, ?, ?)",
                    (sample.source_key, sample.record_id, sample.speaker),
                )
                if cursor.rowcount:
                    accepted[sample.record_id] = sample
            if accepted:
                self._observe_existing(connection, accepted)
                for proposal in proposals:
                    self._save_proposal(connection, proposal, accepted)

    @staticmethod
    def _observe_existing(connection: Connection, samples: dict[int, LanguageSample]) -> None:
        """表达原样再次出现时补证据，保留原有的情景和例句。"""
        entries = connection.execute(
            "SELECT id, text_key FROM language_entries WHERE kind = ?", (LanguageKind.EXPRESSION,)
        ).fetchall()
        imported = [
            (row["instance"], row["source_key"], normalize(row["text"]))
            for row in connection.execute(
                """SELECT instance, source_key, text FROM language_import_rows
                   WHERE table_name = ? AND status = 'received'""",
                (EXPRESSION_TABLE,),
            )
        ]
        for sample in samples.values():
            content = normalize(sample.content)
            connection.executemany(
                "INSERT OR IGNORE INTO language_evidence VALUES (?, ?)",
                [
                    (entry["id"], sample.source_key)
                    for entry in entries
                    if entry["text_key"] in content
                ],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO language_import_observations VALUES (?, ?, ?, ?)",
                [
                    (instance, EXPRESSION_TABLE, key, sample.source_key)
                    for instance, key, text in imported
                    if text in content
                ],
            )

    @staticmethod
    def _save_proposal(
        connection: Connection, proposal: LanguageProposal, samples: dict[int, LanguageSample]
    ) -> None:
        supporting = _supporting_samples(proposal, samples)
        if not supporting:
            return
        keys = (proposal.kind, normalize(proposal.text), normalize(proposal.detail))
        connection.execute(
            """INSERT OR IGNORE INTO language_entries
               (kind, text_key, detail_key, text, detail, example) VALUES (?, ?, ?, ?, ?, ?)""",
            (*keys, proposal.text.strip(), proposal.detail.strip(), supporting[0].content),
        )
        entry_id = connection.execute(
            "SELECT id FROM language_entries WHERE (kind, text_key, detail_key) = (?, ?, ?)", keys
        ).fetchone()["id"]
        connection.executemany(
            "INSERT OR IGNORE INTO language_evidence VALUES (?, ?)",
            [(entry_id, sample.source_key) for sample in supporting],
        )

    def entries(self, kind: LanguageKind) -> list[LanguageEntry]:
        """查询已具备证据的候选，返回关联的时间线编号与说话人。"""
        minimum = MIN_JARGON_EVIDENCE if kind == LanguageKind.JARGON else 1
        with closing(open_connection(self._path)) as connection:
            rows = connection.execute(
                """SELECT entry.id, entry.text, entry.detail, entry.example,
                          observation.record_id, observation.speaker
                   FROM language_entries AS entry
                   JOIN language_evidence AS evidence ON evidence.entry_id = entry.id
                   JOIN language_observations AS observation USING (source_key)
                   WHERE entry.kind = ?
                   ORDER BY entry.id, observation.record_id""",
                (kind,),
            )
            entries: list[LanguageEntry] = []
            for _, group in groupby(rows, key=lambda row: row["id"]):
                evidence = list(group)
                if len(evidence) >= minimum:
                    first = evidence[0]
                    entries.append(
                        LanguageEntry(
                            first["text"],
                            first["detail"],
                            first["example"],
                            len(evidence),
                            tuple(row["record_id"] for row in evidence),
                            frozenset(row["speaker"] for row in evidence),
                        )
                    )
            source_table = JARGON_TABLE if kind == LanguageKind.JARGON else EXPRESSION_TABLE
            entries.extend(imported_entries(connection, source_table))
        return entries

    def jargon_reference(self, texts: Sequence[str], budget: int) -> str:
        """生成 Planner 的黑话参考，按统一库的高频词加权。"""
        with closing(open_connection(self._path)) as connection:
            frequencies = imported_frequencies(connection)
        matches = match_jargons(texts, self.entries(LanguageKind.JARGON), frequencies)
        return format_language(matches, jargon=True, budget=budget)

    def expression_reference(
        self, topic: str, intent: str, avoid: tuple[str, ...], speaker: str, budget: int
    ) -> str:
        """生成 Replyer 的情景表达参考。"""
        matches = select_expressions(
            self.entries(LanguageKind.EXPRESSION), topic, intent, avoid, speaker
        )
        return format_language(matches, jargon=False, budget=budget)
