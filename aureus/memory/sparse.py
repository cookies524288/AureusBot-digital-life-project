"""A_Memorix 2.0.0 的离线检索适配（AGPL-3.0）。

依据 core/storage/metadata_fts.py::fts_search_bm25，将 FTS5 倒排表的
BM25 排名接入 Aureus 的查询。匹配参数、过滤与原件仍由 Aureus 管理。
"""

from collections.abc import Sequence


def keyword_conditions(
    keywords: Sequence[str], table: str, row_id: str
) -> tuple[list[str], list[str]]:
    """生成 D80 的字面条件，表名和行列名由调用方固定。"""
    clauses: list[str] = []
    parameters: list[str] = []
    for keyword in dict.fromkeys(keywords):
        if len(keyword) >= 3:
            clauses.append(f"{row_id} IN (SELECT rowid FROM {table} WHERE {table} MATCH ?)")
            parameters.append('"' + keyword.replace('"', '""') + '"')
        elif len(keyword) == 2:
            clauses.append("content LIKE ? ESCAPE '!'")
            escaped = keyword.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            parameters.append(f"%{escaped}%")
    return clauses, parameters


def ranked_matches(keywords: Sequence[str], table: str) -> tuple[str, str, list[str]]:
    """以一次 MATCH 取 BM25，暂存排名以便和两字查询的 LIKE 合用。"""
    phrases = ['"' + word.replace('"', '""') + '"' for word in keywords if len(word) >= 3]
    if not phrases or any(len(word) == 2 for word in keywords):
        return "", "0.0", []
    return (
        f"WITH ranked AS MATERIALIZED (SELECT rowid, bm25({table}) AS score "
        f"FROM {table} WHERE {table} MATCH ?) ",
        "COALESCE((SELECT score FROM ranked WHERE ranked.rowid = {row_id}), 0.0)",
        [" OR ".join(dict.fromkeys(phrases))],
    )


def relevance_expression(clauses: Sequence[str]) -> str:
    """按命中的不同词数排序，无查询词时视为同等相关。"""
    return " + ".join(f"({clause})" for clause in clauses) if clauses else "0"
