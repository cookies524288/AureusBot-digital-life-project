"""记忆子系统的会话内语言候选、来源和分阶段推断记录。"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import groupby
from sqlite3 import Connection, Row

from aureus.memory.language_matching import LanguageEntry, normalize

INFERENCE_THRESHOLDS = (4, 8, 25, 100)
ONLINE_BATCH_SIZE = 16
ONLINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS language_extractions (
    kind TEXT NOT NULL,
    source_key TEXT NOT NULL REFERENCES language_observations(source_key),
    PRIMARY KEY(kind, source_key)
);
CREATE TABLE IF NOT EXISTS language_online_terms (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    text_key TEXT NOT NULL,
    detail TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(kind, scope, text_key, detail)
);
CREATE TABLE IF NOT EXISTS language_online_evidence (
    term_id INTEGER NOT NULL REFERENCES language_online_terms(id),
    source_key TEXT NOT NULL REFERENCES language_observations(source_key),
    fragment TEXT NOT NULL,
    PRIMARY KEY(term_id, source_key)
);
CREATE TABLE IF NOT EXISTS language_jargon_decisions (
    term_id INTEGER NOT NULL REFERENCES language_online_terms(id),
    evidence_count INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    meaning TEXT NOT NULL,
    PRIMARY KEY(term_id, evidence_count)
);
"""


@dataclass(frozen=True)
class JargonInference:
    """一次达到门槛的词条快照，包含此前真实展示过的会话证据。"""

    term_id: int
    text: str
    evidence_count: int
    evidence: tuple[str, ...]
    previous_meaning: str


def save_online_term(
    connection: Connection,
    kind: str,
    scope: str,
    text: str,
    detail: str,
    sources: Iterable[tuple[str, str]],
) -> int | None:
    """按可信会话分别保存候选和本轮展示的来源前缀。"""
    key = (kind, scope, normalize(text), detail)
    connection.execute(
        "INSERT OR IGNORE INTO language_online_terms VALUES (NULL, ?, ?, ?, ?, ?)",
        (*key, text),
    )
    term_id = connection.execute(
        "SELECT id FROM language_online_terms WHERE (kind, scope, text_key, detail) = (?, ?, ?, ?)",
        key,
    ).fetchone()["id"]
    added = connection.executemany(
        """INSERT OR IGNORE INTO language_online_evidence
           SELECT ?, ?, ?
           WHERE (SELECT count(*) FROM language_online_evidence WHERE term_id = ?) < ?""",
        ((term_id, key, fragment, term_id, INFERENCE_THRESHOLDS[-1]) for key, fragment in sources),
    )
    return term_id if added.rowcount else None


def ready_inferences(connection: Connection, terms: Mapping[int, str]) -> list[JargonInference]:
    """只对本轮增加证据并达到下一档的词条返回推断材料。"""
    attempts: list[JargonInference] = []
    for term_id, text in terms.items():
        rows = _evidence(connection, term_id)
        previous = (
            connection.execute(
                "SELECT max(evidence_count) AS count FROM language_jargon_decisions "
                "WHERE term_id = ?",
                (term_id,),
            ).fetchone()["count"]
            or 0
        )
        if not any(previous < threshold <= len(rows) for threshold in INFERENCE_THRESHOLDS):
            continue
        confirmed = _latest_decision(connection, term_id)
        meaning = confirmed["meaning"] if confirmed is not None else ""
        fragments = tuple(row["fragment"] for row in rows[-ONLINE_BATCH_SIZE:])
        attempts.append(JargonInference(term_id, text, len(rows), fragments, meaning))
    return attempts


def _latest_decision(connection: Connection, term_id: int) -> Row | None:
    return connection.execute(
        """SELECT evidence_count, outcome, meaning FROM language_jargon_decisions
           WHERE term_id = ? AND outcome IN ('accepted', 'similar')
           ORDER BY evidence_count DESC LIMIT 1""",
        (term_id,),
    ).fetchone()


def _evidence(connection: Connection, term_id: int) -> Sequence[Row]:
    return connection.execute(
        """SELECT fragment, record_id, speaker FROM language_online_evidence
           JOIN language_observations USING (source_key) WHERE term_id = ? ORDER BY record_id""",
        (term_id,),
    ).fetchall()


def online_entries(connection: Connection, kind: str) -> Iterator[LanguageEntry]:
    """提供已判定黑话和可回查的表达；来源会话仅作标签。"""
    rows = connection.execute(
        """SELECT term.id, term.text, term.scope, term.detail,
                  evidence.fragment, observation.record_id, observation.speaker
           FROM language_online_terms AS term
           JOIN language_online_evidence AS evidence ON evidence.term_id = term.id
           JOIN language_observations AS observation USING (source_key)
           WHERE term.kind = ? ORDER BY term.id, observation.record_id""",
        (kind,),
    )
    for term_id, group in groupby(rows, key=lambda row: row["id"]):
        evidence = list(group)
        first = evidence[0]
        detail = first["detail"]
        if kind == "jargon":
            decision = _latest_decision(connection, term_id)
            if decision is None or decision["outcome"] != "accepted":
                continue
            detail = decision["meaning"]
        yield LanguageEntry(
            first["text"],
            detail,
            first["fragment"],
            len(evidence),
            tuple(row["record_id"] for row in evidence),
            frozenset(row["speaker"] for row in evidence),
            entry_key=f"online:{term_id}",
            learning_source=first["scope"],
        )
