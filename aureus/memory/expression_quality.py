"""表达独立质检的有限证据，不保存模型原文或聊天正文。"""

from dataclasses import dataclass
from enum import StrEnum
from sqlite3 import Connection

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
