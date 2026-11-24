"""表达独立质检的有限证据，不保存模型原文或聊天正文。"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from sqlite3 import Connection, Row

from aureus.memory.language_matching import normalize
from aureus.memory.language_online import save_online_term

QUALITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS language_expression_quality (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    source_key TEXT NOT NULL REFERENCES language_observations(source_key),
    record_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (scope, source_key, text, detail)
);
CREATE INDEX IF NOT EXISTS language_expression_quality_scope
    ON language_expression_quality (scope, id);
CREATE TABLE IF NOT EXISTS language_expression_quality_terms (
    term_id INTEGER PRIMARY KEY REFERENCES language_online_terms(id),
    is_scoped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS language_expression_quality_actions (
    id INTEGER PRIMARY KEY,
    quality_id INTEGER NOT NULL REFERENCES language_expression_quality(id),
    term_id INTEGER NOT NULL REFERENCES language_expression_quality_terms(term_id),
    action TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (quality_id, action)
);
"""


class ExpressionQualityStatus(StrEnum):
    """模型检查结果，与人工审核结论分开。"""

    PASSED = "passed"
    REJECTED = "rejected"
    ERROR = "error"


class ExpressionQualityReason(StrEnum):
    """将模型的结构化结论映射为简短原因，不存储其自由文本。"""

    MATCHED = "matched"
    MISMATCH = "mismatch"
    TOO_SPECIFIC = "too_specific"
    NO_ROUTE = "no_route"
    BLOCKED = "blocked"
    FAILED = "failed"
    INVALID = "invalid"


@dataclass(frozen=True)
class ExpressionQualityDecision:
    """一次质检取得的建议及记录时刻。"""

    status: ExpressionQualityStatus
    reason: ExpressionQualityReason
    recorded_at: str


class ExpressionQualityAction(StrEnum):
    """维护者对模型拒绝的独立操作。"""

    RESCUED = "rescued"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class ExpressionQualityRecord:
    """可审阅的有限字段，不直接公开底层消息键。"""

    id: int
    scope: str
    text: str
    detail: str
    status: ExpressionQualityStatus
    reason: ExpressionQualityReason
    recorded_at: str
    actions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ExpressionRescuePreview:
    """挽回或撤回的确切原条目及确认时需匹配的版本。"""

    id: int
    scope: str
    text: str
    detail: str
    version: str


type QualitySourceCheck = Callable[[str, str, int], bool]


def browse_quality_records(
    connection: Connection,
    scopes: Sequence[str],
    offset: int,
    limit: int,
    *,
    status: ExpressionQualityStatus | None,
    start: date | None,
    end: date | None,
) -> tuple[list[ExpressionQualityRecord], int]:
    """以已授权的来源集合筛选、计数，再按稳定顺序分页。"""
    query = """FROM language_expression_quality
        WHERE scope IN (SELECT value FROM json_each(?))
          AND (? IS NULL OR status = ?)
          AND (? IS NULL OR date(recorded_at) >= ?)
          AND (? IS NULL OR date(recorded_at) <= ?)"""
    lower = start.isoformat() if start else None
    upper = end.isoformat() if end else None
    args = (json.dumps(scopes), status, status, lower, lower, upper, upper)
    total = connection.execute("SELECT count(*) " + query, args).fetchone()[0]
    rows = connection.execute(
        "SELECT * " + query + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset)
    )
    return [
        ExpressionQualityRecord(
            row["id"],
            row["scope"],
            row["text"],
            row["detail"],
            ExpressionQualityStatus(row["status"]),
            ExpressionQualityReason(row["reason"]),
            row["recorded_at"],
            tuple(
                (action["action"], action["recorded_at"])
                for action in quality_actions(connection, row["id"])
            ),
        )
        for row in rows
    ], total


def quality_actions(connection: Connection, identifier: int) -> list[Row]:
    """按追加顺序读取人工挽回操作，与模型原判断分开。"""
    return connection.execute(
        "SELECT * FROM language_expression_quality_actions WHERE quality_id = ? ORDER BY id",
        (identifier,),
    ).fetchall()


def quality_rescue_target(
    connection: Connection,
    identifier: int,
    action: ExpressionQualityAction,
    source_check: QualitySourceCheck,
) -> tuple[Row, list[Row], ExpressionRescuePreview] | None:
    """选择唯一可信目标；拒绝重复、无稳定原件或与现存表达冲突的挽回。"""
    row = connection.execute(
        """SELECT quality.* FROM language_expression_quality AS quality
           JOIN language_observations AS observation USING (source_key)
           JOIN language_extractions AS extraction USING (source_key)
           WHERE quality.id = ? AND extraction.kind = 'expression'
             AND observation.record_id = quality.record_id AND quality.status = 'rejected'""",
        (identifier,),
    ).fetchone()
    if row is None or not source_check(row["scope"], row["source_key"], row["record_id"]):
        return None
    actions = quality_actions(connection, identifier)
    term = find_expression_term(connection, row["scope"], row["text"], row["detail"])
    if action == ExpressionQualityAction.RESCUED:
        if actions or term is not None:
            return None
    elif len(actions) != 1 or term is None or actions[0]["term_id"] != term:
        return None
    fingerprint = tuple(row) + tuple(tuple(item) for item in actions)
    if term is not None:
        fingerprint += expression_rescue_version(connection, term)
    version = sha256(json.dumps(fingerprint, ensure_ascii=False).encode()).hexdigest()
    return (
        row,
        actions,
        ExpressionRescuePreview(
            identifier,
            row["scope"],
            row["text"],
            row["detail"],
            version,
        ),
    )


def expression_rescue_version(connection: Connection, term_id: int) -> tuple[object, ...]:
    """确认期间表达正文、批准或移除状态变化时使预览失效。"""
    key = f"online:{term_id}"
    edit = connection.execute(
        "SELECT text, detail, example, is_enabled FROM language_edits WHERE entry_key = ?", (key,)
    ).fetchone()
    review = connection.execute(
        "SELECT max(id) FROM language_expression_reviews WHERE entry_key = ?", (key,)
    ).fetchone()[0]
    removed = connection.execute(
        "SELECT 1 FROM language_removed WHERE entry_key = ?", (key,)
    ).fetchone()
    return tuple(edit) if edit else (), review, bool(removed)


def apply_quality_action(
    connection: Connection,
    row: Row,
    actions: Sequence[Row],
    action: ExpressionQualityAction,
    recorded_at: datetime,
) -> str | None:
    """在原判定不变的前提下追加一次狭范围的挽回或撤回。"""
    if action == ExpressionQualityAction.RESCUED:
        term_id = save_quality_term(
            connection,
            row["scope"],
            row["text"],
            row["detail"],
            row["source_key"],
            is_scoped=True,
        )
    else:
        term_id = actions[0]["term_id"]
    if type(term_id) is not int:
        return None
    connection.execute(
        """INSERT INTO language_expression_quality_actions
           (quality_id, term_id, action, recorded_at)
           VALUES (?, ?, ?, ?)""",
        (row["id"], term_id, action, recorded_at.isoformat()),
    )
    return f"online:{term_id}"


def save_quality_decision(
    connection: Connection,
    scope: str,
    source_key: str,
    record_id: int,
    text: str,
    detail: str,
    decision: ExpressionQualityDecision,
) -> None:
    """随来源消费追加一条质检证据。"""
    connection.execute(
        """INSERT INTO language_expression_quality
           (scope, source_key, record_id, text, detail, status, reason, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            scope,
            source_key,
            record_id,
            text,
            detail,
            decision.status,
            decision.reason,
            decision.recorded_at,
        ),
    )


def find_expression_term(connection: Connection, scope: str, text: str, detail: str) -> int | None:
    """以可核对的来源和正文定位旧表达，包括已移除者。"""
    row = connection.execute(
        """SELECT id FROM language_online_terms
           WHERE kind = 'expression' AND scope = ? AND text_key = ? AND detail = ?""",
        (scope, normalize(text), detail),
    ).fetchone()
    return row["id"] if row else None


def save_quality_term(
    connection: Connection,
    scope: str,
    text: str,
    detail: str,
    source_key: str,
    *,
    is_scoped: bool = False,
) -> int | None:
    """给合格条目添加不含原文的证据，新条目保留人工准入闸。"""
    existing = find_expression_term(connection, scope, text, detail)
    term_id = save_online_term(connection, "expression", scope, text, detail, ((source_key, ""),))
    if existing is None and term_id is not None:
        connection.execute(
            "INSERT INTO language_expression_quality_terms VALUES (?, ?)", (term_id, is_scoped)
        )
    return term_id
