"""记忆的维护者覆盖和可重建全文索引。"""

from collections.abc import Sequence
from sqlite3 import Connection
from typing import Literal

from aureus.memory.sparse import keyword_conditions, ranked_matches

type MemoryKind = Literal["history", "live"]

MAX_MEMORY_TEXT = 20000


def open_edits(connection: Connection) -> None:
    """打开覆盖表；缺少派生索引时从覆盖正文重建。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memory_edits (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                entry_id INTEGER NOT NULL,
                edited_content TEXT,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                is_removed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(kind, entry_id)
            )
        """)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_edits)")}
        if "is_removed" not in columns:
            connection.execute(
                "ALTER TABLE memory_edits ADD COLUMN is_removed INTEGER NOT NULL DEFAULT 0"
            )
        if "is_bundle_removed" not in columns:
            connection.execute(
                "ALTER TABLE memory_edits ADD COLUMN is_bundle_removed INTEGER NOT NULL DEFAULT 0"
            )
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'memory_edits_fts'"
            ).fetchone()
            is None
        ):
            connection.execute("""
                CREATE VIRTUAL TABLE memory_edits_fts USING fts5(
                    edited_content, content = 'memory_edits', content_rowid = 'id',
                    tokenize = 'trigram'
                )
            """)
            connection.execute("INSERT INTO memory_edits_fts(memory_edits_fts) VALUES ('rebuild')")
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_edits_insert AFTER INSERT ON memory_edits BEGIN
                INSERT INTO memory_edits_fts(rowid, edited_content)
                    VALUES (new.id, new.edited_content);
            END
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_edits_update
            AFTER UPDATE OF edited_content ON memory_edits BEGIN
                INSERT INTO memory_edits_fts(memory_edits_fts, rowid, edited_content)
                    VALUES ('delete', old.id, old.edited_content);
                INSERT INTO memory_edits_fts(rowid, edited_content)
                    VALUES (new.id, new.edited_content);
            END
        """)


def write_edit(
    connection: Connection,
    kind: MemoryKind,
    entry_id: int,
    text: str | None,
    is_enabled: bool | None,
) -> bool:
    """原子保存维护者覆盖，返回有没有找到原来索引的条目。"""
    if text is not None:
        text = text.strip()
        if not text or len(text) > MAX_MEMORY_TEXT:
            raise ValueError(f"请填写正文，最多 {MAX_MEMORY_TEXT} 字。")
    table = "memory_history" if kind == "history" else "memory_index"
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if (
            connection.execute(f"SELECT 1 FROM {table} WHERE rowid = ?", (entry_id,)).fetchone()
            is None
        ):
            return False
        connection.execute(
            """INSERT INTO memory_edits(kind, entry_id, edited_content, is_enabled)
               VALUES (?, ?, ?, coalesce(?, 1))
               ON CONFLICT(kind, entry_id) DO UPDATE SET
                   edited_content = coalesce(excluded.edited_content, edited_content),
                   is_enabled = coalesce(?, is_enabled)""",
            (kind, entry_id, text, is_enabled, is_enabled),
        )
    return True


def effective_keyword_conditions(
    keywords: Sequence[str],
    index_table: str,
    row_id: str,
    content: str,
) -> tuple[list[str], list[str]]:
    """字面匹配当前正文，原件与修订版分别走对应的索引。"""
    clauses, terms = keyword_conditions(keywords, index_table, row_id, content)
    edits, _ = keyword_conditions(keywords, "memory_edits_fts", "edit.id", "edit.edited_content")
    return [
        f"(CASE WHEN edit.edited_content IS NULL THEN {original} ELSE {revised} END)"
        for original, revised in zip(clauses, edits, strict=True)
    ], [term for term in terms for _ in range(2)]


def effective_ranked_matches(
    keywords: Sequence[str],
    index_table: str,
    row_id: str,
) -> tuple[str, str, list[str]]:
    """让修订版本沿用原件的 BM25 排名规则。"""
    prefix, score, parameters = ranked_matches(keywords, index_table)
    if not prefix:
        return prefix, score, parameters
    edits_prefix, edits_score, edits_parameters = ranked_matches(
        keywords, "memory_edits_fts", name="ranked_edits"
    )
    prefix = prefix.rstrip() + ", " + edits_prefix.removeprefix("WITH ")
    score = (
        "CASE WHEN edit.edited_content IS NULL THEN "
        + score.format(row_id=row_id)
        + " ELSE "
        + edits_score.format(row_id="edit.id")
        + " END"
    )
    return prefix, score, [*parameters, *edits_parameters]
