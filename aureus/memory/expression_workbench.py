"""在权威语言库分页查询工作台表达方式，不加载每条表达的学习证据。"""

import json
from collections.abc import Collection
from dataclasses import dataclass
from functools import partial
from sqlite3 import Connection

from aureus.memory.language_import import EXPRESSION_TABLE, import_entry_key


@dataclass(frozen=True)
class WorkbenchExpression:
    """表达列表一行需要的稳定编号、正文和管理状态。"""

    identifier: int
    entry_key: str
    text: str
    detail: str
    scope: str
    created_at: float | None
    enabled: bool
    removed: bool
    approved: bool


_CATALOG = """
WITH allowed(scope) AS (SELECT value FROM json_each(:scopes)),
sources(entry_key, text, detail, source_scope, term_id, digest) AS (
    SELECT json_array('observed', entry.id), entry.text, entry.detail, NULL, NULL, NULL
    FROM language_entries AS entry
    WHERE entry.kind = 'expression' AND EXISTS (
        SELECT 1 FROM language_evidence AS evidence
        JOIN language_observations USING (source_key) WHERE evidence.entry_id = entry.id
    )
    UNION ALL
    SELECT 'online:' || term.id, term.text, term.detail, term.scope, term.id, NULL
    FROM language_online_terms AS term
    WHERE term.kind = 'expression' AND EXISTS (
        SELECT 1 FROM language_online_evidence AS evidence
        JOIN language_observations USING (source_key) WHERE evidence.term_id = term.id
    )
    UNION ALL
    SELECT expression_import_key(instance, source_key), text, detail, NULL, NULL, NULL
    FROM language_import_rows WHERE table_name = :import_table AND status = 'received'
    UNION ALL
    SELECT 'package:' || digest, text, detail, NULL, NULL, digest
    FROM language_package_rows WHERE kind = 'expression'
    UNION ALL
    SELECT json_array('manual', id), text, detail, NULL, NULL, NULL
    FROM language_manual WHERE kind = 'expression'
),
packages AS (
    SELECT digest, min(allowed.scope) AS visible_scope
    FROM language_package_scopes LEFT JOIN allowed ON allowed.scope = conversation_key
    WHERE conversation_key <> '' GROUP BY digest
),
catalog AS (
    SELECT sources.entry_key,
           coalesce(edit.text, sources.text) AS text,
           CASE WHEN edit.text IS NULL THEN sources.detail ELSE edit.detail END AS detail,
           CASE WHEN meta.entry_key IS NULL THEN sources.source_scope ELSE meta.origin_scope END
               AS origin_scope,
           CASE WHEN quality.is_scoped <> 0 THEN sources.source_scope END AS restricted_scope,
           packages.visible_scope AS package_scope,
           meta.created_at,
           edit.is_enabled IS NOT 0 AND (sources.digest IS NULL OR packages.digest IS NOT NULL)
               AS enabled,
           removed.entry_key IS NOT NULL OR (quality.term_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM language_expression_quality_actions
               WHERE term_id = quality.term_id AND action = 'withdrawn'
           )) AS removed
    FROM sources
    LEFT JOIN language_edits AS edit USING (entry_key)
    LEFT JOIN language_removed AS removed USING (entry_key)
    LEFT JOIN language_expression_quality_terms AS quality USING (term_id)
    LEFT JOIN learning_workbench_metadata AS meta
        ON meta.kind = 'expression' AND meta.entry_key = sources.entry_key
    LEFT JOIN packages USING (digest)
    WHERE packages.digest IS NULL OR packages.visible_scope IS NOT NULL
),
visible AS (
    SELECT *, coalesce(nullif(origin_scope, ''), nullif(restricted_scope, ''), package_scope, '')
        AS scope
    FROM catalog WHERE removed = :removed
        AND (origin_scope IS NULL OR origin_scope IN allowed)
        AND (restricted_scope IS NULL OR restricted_scope IN allowed)
),
unidentified AS (
    SELECT visible.entry_key FROM visible
    WHERE NOT EXISTS (SELECT 1 FROM learning_workbench_ids AS identifiers
        WHERE identifiers.kind = 'expression' AND identifiers.entry_key = visible.entry_key)
),
matched AS (
    SELECT * FROM visible
    WHERE (:targets = '[]' OR scope IN (SELECT value FROM json_each(:targets)))
        AND (:search = '' OR instr(language_casefold(text || ' ' || detail || '  '), :search) > 0)
)
"""


def read_expression_page(
    connection: Connection,
    scopes: Collection[str],
    targets: Collection[str],
    search: str,
    offset: int,
    limit: int,
    *,
    ascending: bool,
    removed: bool,
) -> tuple[list[WorkbenchExpression], int]:
    """在同一事务分配缺失编号、统计并分页；热路径只读，旧编号不更改。"""
    connection.create_function(
        "expression_import_key", 2, partial(import_entry_key, EXPRESSION_TABLE), deterministic=True
    )
    connection.create_function("language_casefold", 1, str.casefold, deterministic=True)
    params = {
        "scopes": json.dumps(list(scopes)),
        "targets": json.dumps(list(targets)),
        "search": search.casefold(),
        "offset": offset,
        "limit": limit,
        "removed": removed,
        "import_table": EXPRESSION_TABLE,
    }
    connection.execute("BEGIN")
    if connection.execute(_CATALOG + "SELECT 1 FROM unidentified LIMIT 1", params).fetchone():
        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            _CATALOG
            + """INSERT INTO learning_workbench_ids(kind, entry_key)
                 SELECT 'expression', entry_key FROM unidentified ORDER BY entry_key""",
            params,
        )
    total = connection.execute(_CATALOG + "SELECT count(*) FROM matched", params).fetchone()[0]
    if offset >= total:
        return [], total
    direction = "ASC" if ascending else "DESC"
    rows = connection.execute(
        _CATALOG
        + f"""SELECT identifiers.id, matched.entry_key, matched.text, matched.detail,
              matched.scope, matched.created_at, matched.enabled,
              (SELECT status FROM language_expression_reviews AS review
               WHERE review.entry_key = matched.entry_key ORDER BY review.id DESC LIMIT 1)
                  = 'approved' AS approved
              FROM matched JOIN learning_workbench_ids AS identifiers USING (entry_key)
              WHERE identifiers.kind = 'expression'
              ORDER BY identifiers.id {direction} LIMIT :limit OFFSET :offset""",
        params,
    )
    return [
        WorkbenchExpression(
            row["id"],
            row["entry_key"],
            row["text"],
            row["detail"],
            row["scope"],
            row["created_at"],
            bool(row["enabled"]) and not removed,
            removed,
            bool(row["approved"]),
        )
        for row in rows
    ], total
