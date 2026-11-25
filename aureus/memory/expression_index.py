"""记忆：表达的派生向量索引与排序，不修改语言原件。

移植 MaiBot 1.3.15 的 expression_vector_index.py（GPL-3.0，见 THIRD_PARTY.md）。
SQLite 替代 JSON/NPZ；键、有效范围及向量计算接 Aureus，不复制上游索引格式。
"""

import json
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from math import sumprod
from pathlib import Path
from sqlite3 import Connection

from aureus.common.sqlite import open_connection
from aureus.memory.expression_clusters import (
    ExpressionClusterMember,
    VisibleExpressionCluster,
    _embedding_text,
    _fingerprint,
    _fit_clusters,
    _unit,
)
from aureus.memory.language import LanguageStore
from aureus.memory.language_matching import LanguageEntry, format_language, normalize

MIN_INDEXED_EXPRESSIONS = 10
EXPRESSION_BACKFILL_BATCH = 200
MAX_VECTOR_CANDIDATES = 50
_CLUSTER_POOL_SIZE = 16
_MAX_CLUSTERS = 80
_ITEM_WEIGHT = 0.875
_CLUSTER_WEIGHT = 0.125
_DIVERSITY_LAMBDA = 0.85

type Vector = tuple[float, ...]


@dataclass(frozen=True)
class IndexedExpression:
    """与当前原件指纹匹配的向量和所属簇。"""

    entry: LanguageEntry
    vector: Vector
    cluster: int


@dataclass(frozen=True)
class ExpressionSpaceCoverage:
    """真实空间当前授权候选的写入覆盖，不暴露条目或模型地址。"""

    space: str
    established: int
    eligible: int
    size_bytes: int = 0
    dimension: int | None = None


class ExpressionVectorIndex:
    """供 D101 本机嵌入路线使用的派生数据。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        with closing(open_connection(path)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS expression_runtime_vectors (
                    space TEXT NOT NULL, entry_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    vector TEXT NOT NULL, dimension INTEGER NOT NULL, cluster INTEGER NOT NULL,
                    PRIMARY KEY (space, entry_key)
                )
            """)

    def missing(self, space: str, entries: Sequence[LanguageEntry]) -> list[LanguageEntry]:
        """只取未索引或内容已变化的候选，不反复嵌入已知内容。"""
        with closing(open_connection(self._path)) as connection:
            known = dict(
                connection.execute(
                    "SELECT entry_key, fingerprint FROM expression_runtime_vectors WHERE space = ?",
                    (space,),
                )
            )
        return [entry for entry in entries if known.get(entry.entry_key) != fingerprint(entry)]

    def discard_incompatible(self, space: str, dimension: int) -> None:
        """清除查询确认不兼容的派生向量，让下次补建重新编码。"""
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute(
                "DELETE FROM expression_runtime_vectors WHERE space = ? AND dimension != ?",
                (space, dimension),
            )

    def publish(
        self, space: str, entries: Sequence[LanguageEntry], raw_vectors: Sequence[Vector]
    ) -> None:
        """原子发布一个批次及新簇；维度变化淘汰该绑定的旧派生向量。"""
        vectors = [_unit(vector, label="表达向量") for vector in raw_vectors]
        if len(vectors) != len(entries) or len({len(vector) for vector in vectors}) != 1:
            raise ValueError("表达向量数量或维度无效。")
        dimension = len(vectors[0])
        with closing(open_connection(self._path)) as connection, connection:
            old_rows = connection.execute(
                "SELECT entry_key, fingerprint, vector FROM expression_runtime_vectors "
                "WHERE space = ? ORDER BY entry_key",
                (space,),
            )
            current = {
                key: (mark, vector)
                for key, mark, encoded in old_rows
                if len(vector := tuple(json.loads(encoded))) == dimension
            }
            current.update(
                (entry.entry_key, (fingerprint(entry), vector))
                for entry, vector in zip(entries, vectors, strict=True)
            )
            count = min(_MAX_CLUSTERS, len(current))
            labels, _ = _fit_clusters([vector for _, vector in current.values()], count)
            connection.execute("DELETE FROM expression_runtime_vectors WHERE space = ?", (space,))
            connection.executemany(
                "INSERT INTO expression_runtime_vectors VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (space, key, mark, json.dumps(vector), dimension, label)
                    for (key, (mark, vector)), label in zip(current.items(), labels, strict=True)
                ),
            )

    def indexed(self, space: str, entries: Sequence[LanguageEntry]) -> list[IndexedExpression]:
        """必须同时匹配当前候选键、实时文本指纹和当前模型空间。"""
        eligible = {entry.entry_key: entry for entry in entries}
        with closing(open_connection(self._path)) as connection:
            rows = connection.execute(
                "SELECT entry_key, fingerprint, vector, cluster FROM expression_runtime_vectors "
                "WHERE space = ? ORDER BY entry_key",
                (space,),
            )
            return [
                IndexedExpression(entry, tuple(json.loads(encoded)), cluster)
                for key, mark, encoded, cluster in rows
                if (entry := eligible.get(key)) is not None and fingerprint(entry) == mark
            ]

    def coverage(
        self, language: LanguageStore, space: str, scopes: frozenset[str]
    ) -> tuple[int, int]:
        """从同一个只读快照统计各授权范围内可选候选的并集。"""
        [row] = self._coverage(language, scopes, space, include_saved=False)
        return row.established, row.eligible

    def spaces(
        self, language: LanguageStore, scopes: frozenset[str], available: str | None
    ) -> list[ExpressionSpaceCoverage]:
        """列运行空间及确有已写向量的历史空间，仅统计当前授权候选。"""
        return self._coverage(language, scopes, available, include_saved=True)

    def _coverage(
        self,
        language: LanguageStore,
        scopes: frozenset[str],
        space: str | None,
        *,
        include_saved: bool,
    ) -> list[ExpressionSpaceCoverage]:
        with closing(open_connection(self._path, read_only=True)) as connection:
            connection.execute("BEGIN")
            candidates = self.candidates(language, scopes, connection)
            eligible = {entry.entry_key: fingerprint(entry) for entry in candidates}
            counts: dict[str, tuple[int, int, int | None]] = (
                {} if space is None else {space: (0, 0, None)}
            )
            if include_saved:
                rows = connection.execute(
                    "SELECT space, entry_key, fingerprint, length(CAST(vector AS BLOB)), dimension "
                    "FROM expression_runtime_vectors "
                    "ORDER BY space"
                )
            else:
                rows = connection.execute(
                    "SELECT space, entry_key, fingerprint, 0, NULL "
                    "FROM expression_runtime_vectors "
                    "WHERE space = ?",
                    (space,),
                )
            for key, entry_key, mark, size, dimension in rows:
                if eligible.get(entry_key) == mark:
                    count, size_bytes, _ = counts.get(key, (0, 0, None))
                    counts[key] = (count + 1, size_bytes + size, dimension)
            return [
                ExpressionSpaceCoverage(key, count, len(eligible), size, dimension)
                for key, (count, size, dimension) in counts.items()
            ]

    @staticmethod
    def candidates(
        language: LanguageStore, scopes: frozenset[str], connection: Connection | None = None
    ) -> list[LanguageEntry]:
        """取普通候选和已授权会话候选的并集，包括仅限原会话的人工挽回表达。"""
        pool = language.expression_pool(connection)
        scoped = {scope for entry in pool for scope in entry.scope_keys}
        scoped.update(entry.restricted_scope for entry in pool if entry.restricted_scope)
        candidates: dict[str, LanguageEntry] = {}
        for scope in (None, *sorted(scopes & scoped)):
            visible = [entry for entry in pool if entry.visible_in(scope)]
            for entry in distinct_expressions(visible, ()):
                candidates[entry.entry_key] = entry
        return list(candidates.values())

    def browse(
        self,
        language: LanguageStore,
        *,
        allowed_entry_keys: Collection[str],
        authorized_versions: Mapping[str, str] | None = None,
    ) -> tuple[VisibleExpressionCluster, ...]:
        """只投影网页明确获准展示且当前指纹有效的运行索引成员。"""
        if not allowed_entry_keys:
            return ()
        with closing(open_connection(self._path, read_only=True)) as connection:
            connection.execute("BEGIN")
            eligible = {
                entry.entry_key: entry
                for entry in language.expression_pool(connection)
                if entry.entry_key in allowed_entry_keys
            }
            rows = connection.execute(
                "SELECT space, cluster, entry_key, fingerprint FROM expression_runtime_vectors "
                "ORDER BY space, cluster, entry_key"
            )
            clusters: dict[tuple[str, int], list[tuple[LanguageEntry, str]]] = defaultdict(list)
            for space, cluster, key, mark in rows:
                if (
                    (entry := eligible.get(key)) is not None
                    and fingerprint(entry) == mark
                    and (
                        authorized_versions is None
                        or authorized_versions.get(key) == display_version(entry)
                    )
                ):
                    clusters[space, cluster].append((entry, mark))
        return tuple(
            VisibleExpressionCluster(
                _fingerprint(f"{space}:{cluster}:" + ":".join(mark for _, mark in group)),
                tuple(
                    ExpressionClusterMember(entry.text, entry.detail, entry.entry_key)
                    for entry, _ in group
                ),
                space,
                cluster,
            )
            for (space, cluster), group in clusters.items()
        )


def embedding_text(entry: LanguageEntry) -> str:
    """采用上游情景、风格的输入，不包含例句或证据。"""
    return _embedding_text(entry)


def fingerprint(entry: LanguageEntry) -> str:
    """使用本地稳定键及规范化的情景、风格。"""
    return _fingerprint(f"{entry.entry_key}\n{embedding_text(entry)}")


def display_version(entry: LanguageEntry) -> str:
    """授权绑定精确展示内容，不把向量规范化后的文本当作人工确认。"""
    return _fingerprint(json.dumps([entry.entry_key, entry.text, entry.detail], ensure_ascii=False))


def distinct_expressions(
    entries: Sequence[LanguageEntry], avoid: tuple[str, ...]
) -> list[LanguageEntry]:
    """排除避用表达；同情景同风格只保留一个候选。"""
    excluded = tuple(filter(None, map(normalize, avoid)))
    distinct: dict[tuple[str, str], LanguageEntry] = {}
    for entry in entries:
        style, situation = normalize(entry.text), normalize(entry.detail)
        if style and situation and not any(term in style for term in excluded):
            distinct.setdefault((situation, style), entry)
    return list(distinct.values())


def rank_expressions(
    indexed: Sequence[IndexedExpression], raw_query: Vector
) -> list[LanguageEntry]:
    """近簇召回、成员与簇中心加权，再用 MMR 避免候选集中。"""
    query = _unit(raw_query, label="表达查询向量")
    if any(len(item.vector) != len(query) for item in indexed):
        raise ValueError("表达查询与索引向量维度不一致。")
    groups: dict[int, list[IndexedExpression]] = defaultdict(list)
    for item in indexed:
        groups[item.cluster].append(item)
    similarities = {
        cluster: sumprod(
            query,
            _unit(
                tuple(map(sum, zip(*(item.vector for item in group), strict=True))),
                label="表达簇中心",
            ),
        )
        for cluster, group in groups.items()
    }
    pool: list[tuple[float, IndexedExpression]] = []
    for number, cluster in enumerate(sorted(groups, key=similarities.__getitem__, reverse=True), 1):
        pool.extend(
            (
                _ITEM_WEIGHT * sumprod(query, item.vector)
                + _CLUSTER_WEIGHT * similarities[cluster],
                item,
            )
            for item in groups[cluster]
        )
        if number >= _CLUSTER_POOL_SIZE and len(pool) >= MAX_VECTOR_CANDIDATES:
            break
    return _select_by_mmr(pool)


def _select_by_mmr(pool: list[tuple[float, IndexedExpression]]) -> list[LanguageEntry]:
    remaining = dict(enumerate(pool))
    penalties: dict[int, float] = {}
    selected: list[tuple[float, IndexedExpression]] = []
    for _ in range(min(MAX_VECTOR_CANDIDATES, len(pool))):
        best = max(
            remaining,
            key=lambda key: (
                _DIVERSITY_LAMBDA * remaining[key][0]
                - (1 - _DIVERSITY_LAMBDA) * penalties.get(key, 0)
            ),
        )
        match = remaining.pop(best)
        selected.append(match)
        for key, (_, item) in remaining.items():
            similarity = sumprod(item.vector, match[1].vector)
            penalties[key] = max(penalties.get(key, similarity), similarity)
    return [item.entry for _, item in sorted(selected, key=lambda match: match[0], reverse=True)]


def format_vector_expressions(entries: Sequence[LanguageEntry], budget: int) -> str:
    """用原回复参考块和预算格式渲染向量路径的已选候选。"""
    return format_language(entries, jargon=False, budget=budget)
