"""记忆子系统：用情景与风格的文本向量计算表达簇。"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from hashlib import sha256
from math import hypot, isfinite, sumprod
from random import Random

from aureus.memory.language import LanguageStore
from aureus.memory.language_matching import LanguageEntry

_CLUSTER_SEED = 20260621
_MAX_ITERATIONS = 100

type _Vector = tuple[float, ...]
type _EmbedExpressions = Callable[[tuple[str, ...]], Awaitable[tuple[str, Sequence[_Vector]]]]


@dataclass(frozen=True)
class _Member:
    entry_key: str
    fingerprint: str


@dataclass(frozen=True)
class ExpressionClusters:
    """一次同空间聚类快照，只保存成员键与内容指纹。"""

    embedding_space: str
    clusters: tuple[tuple[_Member, ...], ...]

    def recall_members(
        self, store: LanguageStore, conversation_key: str | None
    ) -> dict[int, tuple[LanguageEntry, ...]]:
        """按实时条目状态和已有会话授权投影成员，不以来源过滤召回。"""
        current = {
            entry.entry_key: entry for entry in store.expression_candidates(conversation_key)
        }
        result: dict[int, tuple[LanguageEntry, ...]] = {}
        for cluster_id, cluster in enumerate(self.clusters):
            members = tuple(
                entry
                for member in cluster
                if (entry := current.get(member.entry_key)) is not None
                and _fingerprint(_embedding_text(entry)) == member.fingerprint
            )
            if members:
                result[cluster_id] = members
        return result


async def build_expression_clusters(
    store: LanguageStore,
    embed: _EmbedExpressions,
    *,
    cluster_count: int,
    authorized_entry_keys: Collection[str] = (),
    conversation_key: str | None = None,
) -> ExpressionClusters | None:
    """仅将调用方明确授权给该嵌入器、且当场仍可用的表达送入嵌入器。"""
    if cluster_count < 1:
        raise ValueError("表达簇数必须为正数。")
    authorized = frozenset(authorized_entry_keys)
    if not authorized:
        return None
    entries = await asyncio.to_thread(store.expression_candidates, conversation_key)
    candidates = [entry for entry in entries if entry.entry_key in authorized]
    if not candidates:
        return None
    if cluster_count > len(candidates):
        raise ValueError("表达簇数不能超过可用的表达数。")
    candidates.sort(key=lambda entry: entry.entry_key)
    texts = tuple(_embedding_text(entry) for entry in candidates)
    space, vectors = await embed(texts)
    if not space.strip() or len(vectors) != len(candidates):
        raise ValueError("表达向量空间或数量无效。")
    labels = await asyncio.to_thread(_cluster_vectors, vectors, cluster_count)
    clusters: list[list[_Member]] = [[] for _ in range(cluster_count)]
    for entry, text, label in zip(candidates, texts, labels, strict=True):
        clusters[label].append(_Member(entry.entry_key, _fingerprint(text)))
    return ExpressionClusters(space, tuple(tuple(cluster) for cluster in clusters))


def _embedding_text(entry: LanguageEntry) -> str:
    situation = " ".join(entry.detail.split())
    style = " ".join(entry.text.split())
    return f"情景：{situation}\n风格：{style}"


def _fingerprint(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _unit(vector: _Vector, *, label: str) -> _Vector:
    norm = hypot(*vector)
    if not norm or not isfinite(norm):
        raise ValueError(f"{label}不是有效的非零向量。")
    return tuple(value / norm for value in vector)


def _initial_centers(vectors: Sequence[_Vector], count: int) -> list[_Vector]:
    random = Random(_CLUSTER_SEED)
    selected = [random.randrange(len(vectors))]
    nearest = [2.0] * len(vectors)
    for _ in range(count - 1):
        center = vectors[selected[-1]]
        nearest = [
            min(prior, max(0.0, 1.0 - sumprod(vector, center)))
            for prior, vector in zip(nearest, vectors, strict=True)
        ]
        for position in selected:
            nearest[position] = 0.0
        if any(nearest):
            selected.append(random.choices(range(len(vectors)), weights=nearest, k=1)[0])
        else:
            selected.append(next(index for index in range(len(vectors)) if index not in selected))
    return [vectors[index] for index in selected]


def _assign(vectors: Sequence[_Vector], centers: Sequence[_Vector]) -> list[int]:
    labels: list[int] = []
    scores: list[float] = []
    for vector in vectors:
        score, label = max(
            ((sumprod(vector, center), index) for index, center in enumerate(centers)),
            key=lambda match: match[0],
        )
        labels.append(label)
        scores.append(score)
    counts = Counter(labels)
    for index in range(len(centers)):
        if not counts[index]:
            donor = min(
                (position for position, label in enumerate(labels) if counts[label] > 1),
                key=scores.__getitem__,
            )
            counts[labels[donor]] -= 1
            labels[donor] = index
            counts[index] = 1
    return labels


def _cluster_vectors(raw_vectors: Sequence[_Vector], count: int) -> list[int]:
    if any(len(vector) != len(raw_vectors[0]) for vector in raw_vectors):
        raise ValueError("表达向量维度不一致。")
    vectors = [_unit(vector, label="表达向量") for vector in raw_vectors]
    centers = _initial_centers(vectors, count)
    labels: list[int] = []
    for _ in range(_MAX_ITERATIONS):
        assigned = _assign(vectors, centers)
        if assigned == labels:
            break
        labels = assigned
        groups: list[list[_Vector]] = [[] for _ in range(count)]
        for vector, label in zip(vectors, labels, strict=True):
            groups[label].append(vector)
        centers = [
            _unit(tuple(map(sum, zip(*group, strict=True))), label="表达簇中心") for group in groups
        ]
    return labels
