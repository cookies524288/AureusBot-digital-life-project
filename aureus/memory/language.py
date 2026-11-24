"""记忆子系统的习得语言和原始消息证据（D89）。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from itertools import groupby
from pathlib import Path
from sqlite3 import Connection, Row, connect

from aureus.common.sqlite import open_connection
from aureus.memory.expression_quality import (
    QUALITY_SCHEMA,
    ExpressionQualityAction,
    ExpressionQualityDecision,
    ExpressionQualityRecord,
    ExpressionQualityStatus,
    ExpressionRescuePreview,
    QualitySourceCheck,
    apply_quality_action,
    browse_quality_records,
    quality_rescue_target,
    save_quality_decision,
    save_quality_term,
)
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
    ExpressionReviewStatus,
    JargonStatus,
    LanguageEntry,
    format_language,
    is_reference,
    match_jargons,
    normalize,
    select_expressions,
)
from aureus.memory.language_online import (
    ONLINE_BATCH_SIZE,
    ONLINE_SCHEMA,
    JargonInference,
    online_entries,
    ready_inferences,
    save_online_term,
)
from aureus.memory.learning_workbench_ids import identify as identify_workbench_ids
from aureus.memory.learning_workbench_ids import initialize as initialize_workbench_ids
from aureus.memory.learning_workbench_ids import resolve as resolve_workbench_ids

MAX_TEXT = 80
MAX_DETAIL = 160
MAX_EXAMPLE = 2000
MAX_REVIEW_REASON = 500
BATCH_SIZE = ONLINE_BATCH_SIZE
_MEDIA_MARKERS = ("[表情包", "[图片")
_EXCLUDED_MARKERS = ("SELF", *_MEDIA_MARKERS)
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
CREATE TABLE IF NOT EXISTS language_manual (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL,
    example TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS language_edits (
    entry_key TEXT PRIMARY KEY,
    text TEXT,
    detail TEXT,
    example TEXT,
    is_enabled INTEGER
);
CREATE TABLE IF NOT EXISTS language_package_rows (
    digest TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL,
    example TEXT NOT NULL,
    count INTEGER NOT NULL,
    source_chat_name TEXT NOT NULL,
    original TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS language_removed (entry_key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS language_jargon_reviews (
    id INTEGER PRIMARY KEY,
    entry_key TEXT NOT NULL,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS language_jargon_reviews_entry ON language_jargon_reviews (entry_key, id);
CREATE TABLE IF NOT EXISTS language_package_scopes (
    digest TEXT NOT NULL REFERENCES language_package_rows(digest),
    conversation_key TEXT NOT NULL,
    PRIMARY KEY(digest, conversation_key)
);
CREATE TABLE IF NOT EXISTS language_expression_reviews (
    id INTEGER PRIMARY KEY,
    entry_key TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS language_expression_reviews_entry
    ON language_expression_reviews (entry_key, id);
"""


class LanguageKind(StrEnum):
    """两种语言材料的用途。"""

    EXPRESSION = "expression"
    JARGON = "jargon"


class LanguageTargetError(ValueError):
    """语言发布要求一个已有的 Aureus 运行主库。"""


@dataclass(frozen=True)
class LanguageContent:
    """维护者在网页填写的词、释义或场景及例句。"""

    text: str
    detail: str
    example: str = ""

    def validated(self, *, allow_empty_detail: bool = False) -> LanguageContent:
        """去掉首尾空白，检查必填内容和最大长度。"""
        text, detail, example = self.text.strip(), self.detail.strip(), self.example.strip()
        if not text or (not detail and not allow_empty_detail):
            raise ValueError("请填写词或表达，以及释义或使用场景。")
        if len(text) > MAX_TEXT or len(detail) > MAX_DETAIL or len(example) > MAX_EXAMPLE:
            raise ValueError(
                f"词或表达最多 {MAX_TEXT} 字，释义或场景 {MAX_DETAIL} 字，例句 {MAX_EXAMPLE} 字。"
            )
        return LanguageContent(text, detail, example)


def _local_key(kind: str, entry_id: int) -> str:
    return json.dumps((kind, entry_id), separators=(",", ":"))


@dataclass(frozen=True)
class LanguagePackageRow:
    """待映射的包记录，原有会话标签与原始字段只用于保存和导出。"""

    content: LanguageContent
    count: int
    source_chat_name: str
    original: str

    @property
    def digest(self) -> str:
        """以原始包条目及其会话标签去重，不依赖文件名或导出时间。"""
        return sha256(json.dumps((self.source_chat_name, self.original)).encode()).hexdigest()


@dataclass(frozen=True)
class LanguageSample:
    """来自正常入站消息的样本，时间、原文和可见范围通过时间线序号定位。"""

    source_key: str
    record_id: int
    speaker: str
    content: str
    previous_source_key: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class LanguageProposal:
    """器官建议，证据仍须由记忆子系统核验。"""

    kind: LanguageKind
    text: str
    detail: str
    record_ids: tuple[int, ...]


@dataclass(frozen=True)
class ExpressionReview:
    """一条追加写入的人工审核记录。"""

    status: ExpressionReviewStatus
    reason: str
    recorded_at: str


def _is_learnable(sample: LanguageSample) -> bool:
    return (
        bool(normalize(sample.content))
        and not is_reference(sample.content)
        and sample.speaker != "SELF"
        and not any(marker in sample.content for marker in _MEDIA_MARKERS)
    )


def _supporting_samples(
    proposal: LanguageProposal,
    samples: dict[int, LanguageSample],
    presented_content: Mapping[int, str] | None,
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
        if record_id in samples
        and term
        in normalize(
            samples[record_id].content
            if presented_content is None
            else presented_content.get(record_id, "")
        )
    ]


def _online_text(text: str, limit: int) -> bool:
    return (
        0 < len(text) <= limit
        and any(character.isalnum() for character in text)
        and not is_reference(text)
        and not any(marker in text for marker in _EXCLUDED_MARKERS)
    )


def expression_proposals(
    proposals: Sequence[LanguageProposal], presented: Mapping[int, str]
) -> list[LanguageProposal]:
    """抽取、质检和写入共用同一个决定性表达筛选与稳定去重规则。"""
    candidates: dict[tuple[int, str, str], LanguageProposal] = {}
    for proposal in proposals:
        text, detail = proposal.text.strip(), proposal.detail.strip()
        if (
            proposal.kind != LanguageKind.EXPRESSION
            or not _online_text(text, MAX_TEXT)
            or not _online_text(detail, MAX_DETAIL)
            or normalize(text) == normalize(detail)
        ):
            continue
        for record_id in proposal.record_ids:
            if presented.get(record_id):
                candidates.setdefault(
                    (record_id, normalize(text), detail),
                    LanguageProposal(proposal.kind, text, detail, (record_id,)),
                )
    return list(candidates.values())


class LanguageStore:
    """在同一个运行主库读写习得语言，连接只在当前操作内使用。"""

    def __init__(
        self, path: Path, *, old_source_scope: Callable[[int, str], str | None] | None = None
    ) -> None:
        self._path = path
        self._old_source_scope = old_source_scope

    def _observation(
        self, connection: Connection, sample: LanguageSample
    ) -> tuple[str, int | None] | None:
        row = connection.execute(
            "SELECT record_id FROM language_observations WHERE source_key = ?", (sample.source_key,)
        ).fetchone()
        if row is not None:
            return sample.source_key, row["record_id"]
        if sample.previous_source_key is None:
            return None
        previous = connection.execute(
            "SELECT record_id FROM language_observations WHERE source_key = ?",
            (sample.previous_source_key,),
        ).fetchone()
        if previous is None:
            return None
        old_scope = (
            self._old_source_scope(previous["record_id"], sample.previous_source_key)
            if self._old_source_scope
            else None
        )
        if old_scope is None or sample.scope is None:
            return sample.previous_source_key, None
        if old_scope == sample.scope:
            return sample.previous_source_key, previous["record_id"]
        return None

    def open(self) -> None:
        """初始化语言子系统持有的表。"""
        with closing(open_connection(self._path)) as connection:
            connection.executescript(_SCHEMA + IMPORT_SCHEMA + ONLINE_SCHEMA + QUALITY_SCHEMA)
            initialize_workbench_ids(connection)

    def workbench_ids(self, kind: LanguageKind, keys: Iterable[str]) -> dict[str, int]:
        """返回语言条目的稳定整数编号，不改变语言条目本身。"""
        with closing(open_connection(self._path)) as connection, connection:
            return identify_workbench_ids(connection, kind, keys)

    def resolve_workbench_ids(
        self, kind: LanguageKind, identifiers: Iterable[int]
    ) -> dict[int, str]:
        """解析已分配的工作台编号，目标仍由调用方复验。"""
        with closing(open_connection(self._path)) as connection:
            return resolve_workbench_ids(connection, kind, identifiers)

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
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + _SCHEMA + IMPORT_SCHEMA + ONLINE_SCHEMA + QUALITY_SCHEMA
            )
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
                for sample in unique.values()
                if self._observation(connection, sample) is None
            ]

    def observe(
        self,
        samples: Sequence[LanguageSample],
        proposals: Sequence[LanguageProposal],
        *,
        presented_content: Mapping[int, str] | None = None,
    ) -> None:
        """一次事务记录原文观察；提议只从本轮展示的原文前缀取证。"""
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            accepted: dict[int, LanguageSample] = {}
            for sample in samples:
                if not _is_learnable(sample) or self._observation(connection, sample) is not None:
                    continue
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO language_observations VALUES (?, ?, ?)",
                    (sample.source_key, sample.record_id, sample.speaker),
                )
                if cursor.rowcount:
                    accepted[sample.record_id] = sample
            if accepted:
                self._observe_existing(connection, accepted)
                if presented_content is not None:
                    presented_content = {
                        record_id: content
                        for record_id, content in presented_content.items()
                        if record_id in accepted and accepted[record_id].content.startswith(content)
                    }
                for proposal in proposals:
                    self._save_proposal(connection, proposal, accepted, presented_content)

    def pending_extraction(
        self, kind: LanguageKind, samples: Sequence[LanguageSample]
    ) -> list[LanguageSample]:
        """取尚未被这条提取路消费的真实来源。"""
        unique: dict[str, LanguageSample] = {}
        for sample in samples:
            if _is_learnable(sample):
                unique.setdefault(sample.source_key, sample)
        with closing(open_connection(self._path)) as connection:
            return [
                sample
                for sample in unique.values()
                if (observed := self._observation(connection, sample)) is not None
                and observed[1] == sample.record_id
                and connection.execute(
                    "SELECT 1 FROM language_extractions WHERE kind = ? AND source_key = ?",
                    (kind, observed[0]),
                ).fetchone()
                is None
            ]

    def observe_extraction(
        self,
        kind: LanguageKind,
        scope: str,
        samples: Sequence[LanguageSample],
        presented: Mapping[int, str],
        proposals: Sequence[LanguageProposal],
        *,
        quality: Mapping[LanguageProposal, ExpressionQualityDecision] | None = None,
    ) -> list[JargonInference]:
        """独立消费成功的抽取，并返回本轮达到门槛的黑话候选。"""
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            accepted: dict[int, tuple[str, str]] = {}
            for sample in samples:
                fragment = presented.get(sample.record_id, "")
                if (
                    not fragment
                    or not sample.content.startswith(fragment)
                    or not _is_learnable(sample)
                ):
                    continue
                observation = self._observation(connection, sample)
                if observation is None or observation[1] != sample.record_id:
                    continue
                source_key = observation[0]
                changed = connection.execute(
                    "INSERT OR IGNORE INTO language_extractions VALUES (?, ?)", (kind, source_key)
                )
                if changed.rowcount:
                    accepted[sample.record_id] = (source_key, fragment)
            if kind == LanguageKind.EXPRESSION:
                self._save_expressions(connection, scope, accepted, proposals, quality)
                return []
            return self._save_jargons(connection, scope, accepted, proposals)

    @staticmethod
    def _save_expressions(
        connection: Connection,
        scope: str,
        accepted: Mapping[int, tuple[str, str]],
        proposals: Sequence[LanguageProposal],
        quality: Mapping[LanguageProposal, ExpressionQualityDecision] | None,
    ) -> None:
        candidates = expression_proposals(
            proposals, {record_id: fragment for record_id, (_, fragment) in accepted.items()}
        )
        for candidate in candidates:
            record_id = candidate.record_ids[0]
            source_key, fragment = accepted[record_id]
            if quality is None:
                save_online_term(
                    connection,
                    candidate.kind,
                    scope,
                    candidate.text,
                    candidate.detail,
                    ((source_key, fragment),),
                )
                continue
            decision = quality.get(candidate)
            if decision is None:
                continue
            save_quality_decision(
                connection, scope, source_key, record_id, candidate.text, candidate.detail, decision
            )
            if decision.status == ExpressionQualityStatus.PASSED:
                save_quality_term(connection, scope, candidate.text, candidate.detail, source_key)

    @staticmethod
    def _save_jargons(
        connection: Connection,
        scope: str,
        accepted: Mapping[int, tuple[str, str]],
        proposals: Sequence[LanguageProposal],
    ) -> list[JargonInference]:
        terms: dict[int, str] = {}
        for proposal in proposals:
            text, detail = proposal.text.strip(), proposal.detail.strip()
            if proposal.kind != LanguageKind.JARGON or not _online_text(text, MAX_TEXT):
                continue
            sources = [
                accepted[record_id]
                for record_id in dict.fromkeys(proposal.record_ids)
                if record_id in accepted and normalize(text) in normalize(accepted[record_id][1])
            ]
            if sources:
                term = save_online_term(connection, proposal.kind, scope, text, detail, sources)
                if term is not None:
                    terms[term] = text
        return ready_inferences(connection, terms)

    def record_jargon_inference(
        self, attempt: JargonInference, outcome: str, meaning: str = ""
    ) -> None:
        """追加本档的有效判定；未能推断不改变此前生效的释义。"""
        if outcome == "accepted" and (
            not _online_text(meaning, MAX_DETAIL) or normalize(attempt.text) == normalize(meaning)
        ):
            outcome, meaning = "insufficient", ""
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO language_jargon_decisions VALUES (?, ?, ?, ?)",
                (attempt.term_id, attempt.evidence_count, outcome, meaning),
            )

    def expression_quality_page(
        self,
        scopes: Sequence[str],
        offset: int,
        limit: int,
        *,
        status: ExpressionQualityStatus | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[list[ExpressionQualityRecord], int]:
        """在同一个读事务中查询已获准来源的质检记录和计数。"""
        with closing(open_connection(self._path)) as connection:
            connection.execute("BEGIN")
            return browse_quality_records(
                connection, scopes, offset, limit, status=status, start=start, end=end
            )

    def maintain_expression_quality(
        self,
        identifier: int,
        action: ExpressionQualityAction,
        source_check: QualitySourceCheck,
        now: datetime,
        *,
        version: str | None = None,
    ) -> ExpressionRescuePreview | str | None:
        """无版本只预览，确认时以当前授权及目标版本复核后原子追加。"""
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE" if version is not None else "BEGIN")
            target = quality_rescue_target(connection, identifier, action, source_check)
            if target is None:
                return None
            row, actions, preview = target
            if version is None:
                return preview
            if version != preview.version:
                return None
            return apply_quality_action(connection, row, actions, action, now)

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
        connection: Connection,
        proposal: LanguageProposal,
        samples: dict[int, LanguageSample],
        presented_content: Mapping[int, str] | None,
    ) -> None:
        supporting = _supporting_samples(proposal, samples, presented_content)
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

    def add_entry(
        self,
        kind: LanguageKind,
        content: LanguageContent,
        *,
        origin_scope: str | None = None,
        recorded_at: datetime | None = None,
    ) -> str:
        """新增一条维护者指定的语言参考，返回稳定标识。"""
        content = content.validated(allow_empty_detail=kind == LanguageKind.JARGON)
        with closing(open_connection(self._path)) as connection, connection:
            row = connection.execute(
                "INSERT INTO language_manual (kind, text, detail, example) VALUES (?, ?, ?, ?) "
                "RETURNING id",
                (kind, content.text, content.detail, content.example),
            ).fetchone()
            key = _local_key("manual", row["id"])
            if recorded_at is not None:
                connection.execute(
                    "INSERT INTO learning_workbench_metadata VALUES (?, ?, ?, ?)",
                    (kind, key, origin_scope, recorded_at.timestamp()),
                )
            return key

    def workbench_metadata(self, kind: LanguageKind) -> dict[str, dict[str, object]]:
        """读取工作台实际记载的来源及时间，存量缺失值保持缺失。"""
        with closing(open_connection(self._path)) as connection:
            return {
                row["entry_key"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM learning_workbench_metadata WHERE kind = ?", (kind,)
                )
            }

    def save_package(self, kind: LanguageKind, rows: Sequence[LanguagePackageRow]) -> int:
        """一次追加整包待映射原件，不修改任何现有条目或覆盖。"""
        with closing(open_connection(self._path)) as connection, connection:
            return connection.executemany(
                """INSERT INTO language_package_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(digest) DO NOTHING""",
                (
                    (
                        row.digest,
                        kind,
                        row.content.text,
                        row.content.detail,
                        row.content.example,
                        row.count,
                        row.source_chat_name,
                        row.original,
                    )
                    for row in rows
                ),
            ).rowcount

    def package_originals(self, kind: LanguageKind) -> dict[str, tuple[str, str]]:
        """供导出查找待映射条目的原始作用域标签和资源包字段。"""
        with closing(open_connection(self._path)) as connection:
            rows = connection.execute(
                "SELECT digest, source_chat_name, original FROM language_package_rows "
                "WHERE kind = ?",
                (kind,),
            )
            return {
                f"package:{row['digest']}": (row["source_chat_name"], row["original"])
                for row in rows
            }

    def package_scope(
        self,
        kind: LanguageKind,
        entry_key: str,
        conversation_keys: Sequence[str],
        *,
        confirm: bool,
    ) -> tuple[str, ...] | None:
        """返回原来的目标范围；明确确认时仅改包记录的会话授权。"""
        prefix = "package:"
        if not entry_key.startswith(prefix):
            return None
        digest = entry_key.removeprefix(prefix)
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE" if confirm else "BEGIN")
            if (
                connection.execute(
                    "SELECT 1 FROM language_package_rows WHERE kind = ? AND digest = ?",
                    (kind, digest),
                ).fetchone()
                is None
            ):
                return None
            previous = tuple(
                row["conversation_key"]
                for row in connection.execute(
                    "SELECT conversation_key FROM language_package_scopes "
                    "WHERE digest = ? ORDER BY conversation_key",
                    (digest,),
                )
            )
            if confirm:
                connection.execute(
                    "DELETE FROM language_package_scopes WHERE digest = ?", (digest,)
                )
                connection.executemany(
                    "INSERT INTO language_package_scopes VALUES (?, ?)",
                    ((digest, key) for key in sorted(set(conversation_keys))),
                )
        return previous

    def set_removed(self, kind: LanguageKind, keys: Sequence[str], removed: bool) -> bool:
        """标记整批条目移除或恢复，不触碰原件及原有召回设置。"""
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not set(keys) <= {entry.entry_key for entry in self._read_entries(connection, kind)}:
                return False
            statement = (
                "INSERT OR IGNORE INTO language_removed VALUES (?)"
                if removed
                else "DELETE FROM language_removed WHERE entry_key = ?"
            )
            connection.executemany(statement, ((key,) for key in keys))
        return True

    def classify_jargons(
        self, keys: Sequence[str], status: JargonStatus, now: datetime
    ) -> int | None:
        """锁定后复验整批条目，只记载真正变化的定性。"""
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            entries = {
                entry.entry_key: entry
                for entry in self._catalog_entries(
                    connection, LanguageKind.JARGON, include_disabled=True
                )
                if not entry.is_removed
            }
            if not set(keys) <= entries.keys():
                return None
            return connection.executemany(
                """INSERT INTO language_jargon_reviews (entry_key, status, recorded_at)
                   VALUES (?, ?, ?)""",
                (
                    (key, status, now.isoformat())
                    for key in dict.fromkeys(keys)
                    if entries[key].jargon_status != status
                ),
            ).rowcount

    def review_expressions(
        self,
        keys: Sequence[str],
        status: ExpressionReviewStatus,
        reason: str,
        recorded_at: datetime,
    ) -> int:
        """为整批表达追加人工审核；任何目标不存在时不写入。"""
        reason = reason.strip()
        if status == ExpressionReviewStatus.REJECTED and not reason:
            raise ValueError("请填写拒绝理由。")
        if len(reason) > MAX_REVIEW_REASON:
            raise ValueError(f"审核理由最多 {MAX_REVIEW_REASON} 字。")
        targets = set(keys)
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            entries = self._read_entries(connection, LanguageKind.EXPRESSION)
            if not targets <= {entry.entry_key for entry in entries}:
                return 0
            timestamp = recorded_at.isoformat()
            connection.executemany(
                """INSERT INTO language_expression_reviews (entry_key, status, reason, recorded_at)
                   VALUES (?, ?, ?, ?)""",
                ((key, status, reason, timestamp) for key in targets),
            )
        return len(targets)

    def expression_reviews(self, entry_key: str) -> list[ExpressionReview]:
        """从新到旧读取单条表达的人工审核历史。"""
        with closing(open_connection(self._path)) as connection:
            rows = connection.execute(
                """SELECT status, reason, recorded_at FROM language_expression_reviews
                   WHERE entry_key = ? ORDER BY id DESC""",
                (entry_key,),
            )
            return [
                ExpressionReview(
                    ExpressionReviewStatus(row["status"]), row["reason"], row["recorded_at"]
                )
                for row in rows
            ]

    def update_entry(
        self,
        kind: LanguageKind,
        entry_key: str,
        content: LanguageContent | None,
        is_enabled: bool | None,
    ) -> bool:
        """保存指定条目的内容或启停覆盖，返回条目是否存在。"""
        if content is not None:
            content = content.validated(allow_empty_detail=kind == LanguageKind.JARGON)
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            entry = next(
                (
                    entry
                    for entry in self._read_entries(connection, kind)
                    if entry.entry_key == entry_key
                ),
                None,
            )
            if entry is None:
                return False
            if entry.pending_scope and is_enabled:
                raise ValueError("包记录尚未映射会话，不能参与召回。")
            connection.execute(
                """INSERT INTO language_edits (entry_key, text, detail, example, is_enabled)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(entry_key) DO UPDATE SET
                       text = coalesce(excluded.text, text),
                       detail = coalesce(excluded.detail, detail),
                       example = coalesce(excluded.example, example),
                       is_enabled = coalesce(excluded.is_enabled, is_enabled)""",
                (
                    entry_key,
                    content.text if content else None,
                    content.detail if content else None,
                    content.example if content else None,
                    is_enabled,
                ),
            )
        return True

    def entries(self, kind: LanguageKind, *, include_disabled: bool = False) -> list[LanguageEntry]:
        """返回可用语言和关联证据，管理页可以同时浏览停用条目。"""
        with closing(open_connection(self._path)) as connection:
            connection.execute("BEGIN")
            return self._catalog_entries(connection, kind, include_disabled=include_disabled)

    def _catalog_entries(
        self, connection: Connection, kind: LanguageKind, *, include_disabled: bool = False
    ) -> list[LanguageEntry]:
        entries = self._read_entries(connection, kind)
        edits = {
            row["entry_key"]: row for row in connection.execute("SELECT * FROM language_edits")
        }
        removed = {row["entry_key"] for row in connection.execute("SELECT * FROM language_removed")}
        if kind == LanguageKind.EXPRESSION:
            reviews = {
                row["entry_key"]: ExpressionReviewStatus(row["status"])
                for row in connection.execute(
                    """SELECT entry_key, status, max(id) FROM language_expression_reviews
                       GROUP BY entry_key"""
                )
            }
            checked = {
                f"online:{row['term_id']}": row
                for row in connection.execute(
                    """SELECT term_id, is_scoped,
                       EXISTS (SELECT 1 FROM language_expression_quality_actions AS action
                       WHERE action.term_id = term.term_id AND action.action = 'withdrawn')
                       AS is_withdrawn FROM language_expression_quality_terms AS term"""
                )
            }
            removed.update(key for key, row in checked.items() if row["is_withdrawn"])
            entries = [
                replace(
                    entry,
                    review_status=reviews.get(entry.entry_key, ExpressionReviewStatus.PENDING),
                    requires_approval=entry.entry_key in checked,
                    restricted_scope=(
                        entry.learning_source
                        if entry.entry_key in checked and checked[entry.entry_key]["is_scoped"]
                        else None
                    ),
                )
                for entry in entries
            ]
        else:
            classifications = {
                row["entry_key"]: JargonStatus(row["status"])
                for row in connection.execute(
                    """SELECT entry_key, status, max(id) FROM language_jargon_reviews
                       GROUP BY entry_key"""
                )
            }
            entries = [
                replace(
                    entry, jargon_status=classifications.get(entry.entry_key, entry.jargon_status)
                )
                for entry in entries
            ]
        entries = [self._apply_edit(entry, edits.get(entry.entry_key)) for entry in entries]
        origins = {
            row["entry_key"]: row["origin_scope"]
            for row in connection.execute(
                "SELECT entry_key, origin_scope FROM learning_workbench_metadata WHERE kind = ?",
                (kind,),
            )
        }
        entries = [
            replace(entry, learning_source=origins[entry.entry_key])
            if entry.entry_key in origins
            else entry
            for entry in entries
        ]
        entries = [
            replace(entry, is_enabled=False, is_removed=True)
            if entry.entry_key in removed
            else entry
            for entry in entries
        ]
        if include_disabled:
            return entries
        if kind == LanguageKind.JARGON:
            return [entry for entry in entries if entry.can_reference_jargon()]
        return [entry for entry in entries if entry.is_enabled]

    def browse_page(
        self,
        kind: LanguageKind,
        offset: int,
        limit: int,
        *,
        conversation_key: str = "",
        origin_conversation_key: str = "",
        authorized_conversation_key: str = "",
        text: str = "",
        is_enabled: bool | None = None,
        review: ExpressionReviewStatus | None = None,
        jargon_status: JargonStatus | None = None,
        shared: bool = False,
        is_removed: bool = False,
    ) -> tuple[list[LanguageEntry], int]:
        """分别筛选可信来源和明确授权，兼容旧调用的并集语义。"""
        term = text.strip().casefold()
        matches = [
            entry
            for entry in self.entries(kind, include_disabled=True)
            if (
                not conversation_key
                or conversation_key == entry.learning_source
                or conversation_key in entry.scope_keys
            )
            and (not origin_conversation_key or origin_conversation_key == entry.learning_source)
            and (not authorized_conversation_key or authorized_conversation_key in entry.scope_keys)
            and entry.is_removed == is_removed
            and (is_enabled is None or entry.is_enabled == is_enabled)
            and (review is None or entry.review_status == review)
            and (jargon_status is None or entry.jargon_status == jargon_status)
            and (not shared or (entry.package_source is None and entry.restricted_scope is None))
            and (
                not term
                or any(
                    term in field.casefold()
                    for field in (entry.text, entry.detail, entry.example, *(entry.original or ()))
                )
            )
        ]
        return matches[offset : offset + limit], len(matches)

    @staticmethod
    def _apply_edit(entry: LanguageEntry, row: Row | None) -> LanguageEntry:
        if row is None:
            return entry
        entry = replace(entry, is_enabled=not entry.pending_scope and row["is_enabled"] != 0)
        if row["text"] is None:
            return entry
        return replace(
            entry,
            text=row["text"],
            detail=row["detail"],
            example=row["example"],
            original=(entry.text, entry.detail, entry.example),
        )

    @staticmethod
    def _read_entries(connection: Connection, kind: LanguageKind) -> list[LanguageEntry]:
        minimum = MIN_JARGON_EVIDENCE if kind == LanguageKind.JARGON else 1
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
        for entry_id, group in groupby(rows, key=lambda row: row["id"]):
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
                        entry_key=_local_key("observed", entry_id),
                        jargon_status=JargonStatus.CONFIRMED
                        if kind == LanguageKind.JARGON
                        else None,
                    )
                )
        source_table = JARGON_TABLE if kind == LanguageKind.JARGON else EXPRESSION_TABLE
        entries.extend(online_entries(connection, kind))
        entries.extend(imported_entries(connection, source_table))
        entries.extend(LanguageStore._package_entries(connection, kind))
        entries.extend(
            LanguageEntry(
                row["text"],
                row["detail"],
                row["example"],
                0,
                (),
                frozenset(),
                entry_key=_local_key("manual", row["id"]),
                jargon_status=(
                    None
                    if kind != LanguageKind.JARGON
                    else JargonStatus.CONFIRMED
                    if row["detail"]
                    else JargonStatus.CANDIDATE
                ),
            )
            for row in connection.execute(
                "SELECT * FROM language_manual WHERE kind = ? ORDER BY id DESC", (kind,)
            )
        )
        return entries

    @staticmethod
    def _package_entries(connection: Connection, kind: LanguageKind) -> list[LanguageEntry]:
        rows = connection.execute(
            """SELECT package.*, scope.conversation_key FROM language_package_rows AS package
               LEFT JOIN language_package_scopes AS scope USING (digest)
               WHERE kind = ? ORDER BY digest, conversation_key""",
            (kind,),
        )
        entries: list[LanguageEntry] = []
        for digest, group in groupby(rows, key=lambda row: row["digest"]):
            sources = list(group)
            row = sources[0]
            keys = tuple(
                source["conversation_key"] for source in sources if source["conversation_key"]
            )
            entries.append(
                LanguageEntry(
                    row["text"],
                    row["detail"],
                    row["example"],
                    row["count"],
                    (),
                    frozenset(),
                    entry_key=f"package:{digest}",
                    is_enabled=bool(keys),
                    pending_scope=not keys,
                    scope_keys=keys,
                    package_source=row["source_chat_name"],
                    jargon_status=(
                        _packaged_jargon_status(row["original"])
                        if kind == LanguageKind.JARGON
                        else None
                    ),
                )
            )
        return entries

    def jargon_reference(
        self, texts: Sequence[str], budget: int, *, conversation_key: str | None = None
    ) -> str:
        """生成规划与回复的黑话参考，按统一库的高频词加权。"""
        with closing(open_connection(self._path)) as connection:
            frequencies = imported_frequencies(connection)
        entries = self._visible_entries(LanguageKind.JARGON, conversation_key)
        matches = match_jargons(texts, entries, frequencies)
        return format_language(matches, jargon=True, budget=budget)

    def expression_reference(
        self,
        topic: str,
        intent: str,
        avoid: tuple[str, ...],
        speaker: str,
        budget: int,
        *,
        conversation_key: str | None = None,
    ) -> str:
        """生成 Replyer 的情景表达参考。"""
        entries = self.expression_candidates(conversation_key)
        matches = select_expressions(entries, topic, intent, avoid, speaker)
        return format_language(matches, jargon=False, budget=budget)

    def expression_candidates(self, conversation_key: str | None) -> list[LanguageEntry]:
        """返回某一会话可用的表达；来源不限制在线记录的召回。"""
        return [entry for entry in self.expression_pool() if entry.visible_in(conversation_key)]

    def expression_pool(self, connection: Connection | None = None) -> list[LanguageEntry]:
        """在应用会话授权前读取有效表达；可在调用方的同库读事务中取值。"""
        entries = (
            self.entries(LanguageKind.EXPRESSION)
            if connection is None
            else self._catalog_entries(connection, LanguageKind.EXPRESSION)
        )
        return [entry for entry in entries if entry.can_reference_expression()]

    def _visible_entries(
        self, kind: LanguageKind, conversation_key: str | None
    ) -> list[LanguageEntry]:
        return [entry for entry in self.entries(kind) if entry.visible_in(conversation_key)]


def _packaged_jargon_status(original: str) -> JargonStatus:
    fields = json.loads(original)
    if fields.get("aureus_jargon_status") == JargonStatus.CANDIDATE:
        return JargonStatus.CANDIDATE
    return JargonStatus.CONFIRMED if fields.get("is_jargon", True) else JargonStatus.NON_JARGON
