"""使用合成表达和假文本向量器官验收聚类与召回边界。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.memory import (
    LanguageContent,
    LanguageKind,
    LanguageProposal,
    LanguageSample,
    LanguageStore,
)
from aureus.memory.expression_clusters import ExpressionClusters, build_expression_clusters
from aureus.memory.language import LanguagePackageRow
from aureus.memory.language_matching import ExpressionReviewStatus
from aureus.organs import OrganRequest, OrganResponse

EXPRESSION = LanguageKind.EXPRESSION
PRIVATE = "synthetic:private"
GROUP = "synthetic:group"
OTHER = "synthetic:other"


class FakeExpressionOrgan:
    def __init__(self, vectors: dict[str, tuple[float, ...]]) -> None:
        self.vectors = vectors
        self.requests: list[OrganRequest] = []

    async def process(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        return OrganResponse(
            "text_embedding",
            "",
            metadata={"embedding_space": "synthetic-expression-space"},
            embeddings=tuple(self.vectors[str(text)] for text in request.embedding_inputs),
        )

    async def embed(self, texts: tuple[str, ...]) -> tuple[str, tuple[tuple[float, ...], ...]]:
        response = await self.process(OrganRequest("text_embedding", "", embedding_inputs=texts))
        return str(response.metadata["embedding_space"]), response.embeddings


@pytest.fixture
def store(tmp_path: Path) -> LanguageStore:
    language = LanguageStore(tmp_path / "synthetic-language.sqlite")
    language.open()
    return language


def _embedding_text(situation: str, style: str) -> str:
    return f"情景：{situation}\n风格：{style}"


def _member_keys(clusters: ExpressionClusters, store: LanguageStore) -> set[frozenset[str]]:
    return {
        frozenset(entry.entry_key for entry in members)
        for members in clusters.recall_members(store, GROUP).values()
    }


@pytest.mark.asyncio
async def test_expression_clusters_do_not_embed_without_explicit_permission(
    store: LanguageStore,
) -> None:
    store.add_entry(EXPRESSION, LanguageContent("合成未授权", "本地可召回"))
    package = LanguagePackageRow(
        LanguageContent("合成语言包", "仅允许会话召回"), 1, "只作来源", "{}"
    )
    assert store.save_package(EXPRESSION, [package]) == 1
    package_key = f"package:{package.digest}"
    assert store.package_scope(EXPRESSION, package_key, [GROUP], confirm=True) == ()
    fake = FakeExpressionOrgan(
        {
            _embedding_text("本地可召回", "合成未授权"): (1, 0),
            _embedding_text("仅允许会话召回", "合成语言包"): (1, 1),
        }
    )
    clusters = await build_expression_clusters(store, fake.embed, cluster_count=1)
    assert [request.embedding_inputs for request in fake.requests] == []
    assert clusters is None


@pytest.mark.asyncio
async def test_expression_cluster_embedding_inputs_require_live_scope_and_permission(
    store: LanguageStore,
) -> None:
    allowed = store.add_entry(EXPRESSION, LanguageContent("明示许可", "允许材料"))
    disabled = store.add_entry(EXPRESSION, LanguageContent("停用", "不得发送"))
    removed = store.add_entry(EXPRESSION, LanguageContent("移除", "不得发送"))
    rejected = store.add_entry(EXPRESSION, LanguageContent("旧审核拒绝", "旧审核不影响授权"))
    store.add_entry(EXPRESSION, LanguageContent("未许可", "不得发送"))
    assert store.update_entry(EXPRESSION, disabled, None, False)
    assert store.set_removed(EXPRESSION, [removed], True)
    stamp = datetime(2026, 11, 24, tzinfo=UTC)
    assert store.review_expressions([rejected], ExpressionReviewStatus.REJECTED, "不采用", stamp)
    package = LanguagePackageRow(
        LanguageContent("包材料", "包内情景"), 1, "只作来源", '{"is_global":true}'
    )
    assert store.save_package(EXPRESSION, [package]) == 1
    package_key = f"package:{package.digest}"
    allowed_input = _embedding_text("允许材料", "明示许可")
    package_input = _embedding_text("包内情景", "包材料")
    rejected_input = _embedding_text("旧审核不影响授权", "旧审核拒绝")
    fake = FakeExpressionOrgan(
        {allowed_input: (1, 0), rejected_input: (1, 0), package_input: (1, 1)}
    )
    authorized = (allowed, disabled, removed, rejected, package_key)

    async def build(conversation_key: str | None) -> None:
        clusters = await build_expression_clusters(
            store,
            fake.embed,
            cluster_count=1,
            authorized_entry_keys=authorized,
            conversation_key=conversation_key,
        )
        assert clusters is not None

    await build(GROUP)
    assert store.package_scope(EXPRESSION, package_key, [GROUP], confirm=True) == ()
    await build(None)
    await build(OTHER)
    await build(GROUP)
    assert store.package_scope(EXPRESSION, package_key, [], confirm=True) == (GROUP,)
    await build(GROUP)
    assert [set(request.embedding_inputs) for request in fake.requests] == [
        {allowed_input, rejected_input},
        {allowed_input, rejected_input},
        {allowed_input, rejected_input},
        {allowed_input, rejected_input, package_input},
        {allowed_input, rejected_input},
    ]


@pytest.mark.asyncio
async def test_expression_clusters_follow_vector_direction_and_keep_homonyms(
    store: LanguageStore,
) -> None:
    first = store.add_entry(EXPRESSION, LanguageContent("同名", "场景  甲", "合成原句，不可嵌入"))
    second = store.add_entry(EXPRESSION, LanguageContent("同名", "场景 乙"))
    third = store.add_entry(EXPRESSION, LanguageContent("独立说法", "场景 丙"))
    fourth = store.add_entry(EXPRESSION, LanguageContent("另一说法", "场景 丁"))
    store.add_entry(LanguageKind.JARGON, LanguageContent("不选黑话", "不是表达"))
    inputs = (
        _embedding_text("场景 甲", "同名"),
        _embedding_text("场景 乙", "同名"),
        _embedding_text("场景 丙", "独立说法"),
        _embedding_text("场景 丁", "另一说法"),
    )
    fake = FakeExpressionOrgan(dict(zip(inputs, [(0, 2), (20, 1), (1, 20), (2, 0)], strict=True)))
    authorized = (first, second, third, fourth)
    clusters = await build_expression_clusters(
        store, fake.embed, cluster_count=2, authorized_entry_keys=authorized
    )
    assert clusters is not None
    assert clusters.embedding_space == "synthetic-expression-space"
    assert _member_keys(clusters, store) == {frozenset((first, third)), frozenset((second, fourth))}
    [request] = fake.requests
    assert request.embedding_inputs == inputs

    fake.vectors[inputs[2]], fake.vectors[inputs[3]] = (
        fake.vectors[inputs[3]],
        fake.vectors[inputs[2]],
    )
    permuted = await build_expression_clusters(
        store, fake.embed, cluster_count=2, authorized_entry_keys=authorized
    )
    assert permuted is not None
    assert _member_keys(permuted, store) == {frozenset((first, fourth)), frozenset((second, third))}


@pytest.mark.asyncio
async def test_expression_cluster_recall_rechecks_state_and_content(store: LanguageStore) -> None:
    key = store.add_entry(EXPRESSION, LanguageContent("同名", "合成场景"))
    fake = FakeExpressionOrgan({_embedding_text("合成场景", "同名"): (1, 0)})

    async def build() -> ExpressionClusters | None:
        return await build_expression_clusters(
            store, fake.embed, cluster_count=1, authorized_entry_keys=(key,)
        )

    clusters = await build()
    assert clusters is not None
    assert _member_keys(clusters, store) == {frozenset((key,))}

    assert store.update_entry(EXPRESSION, key, None, False)
    assert not clusters.recall_members(store, GROUP)
    assert await build() is None
    assert store.set_removed(EXPRESSION, [key], True)
    assert store.set_removed(EXPRESSION, [key], False)
    assert not clusters.recall_members(store, GROUP)
    assert store.update_entry(EXPRESSION, key, None, True)
    assert _member_keys(clusters, store) == {frozenset((key,))}
    assert store.set_removed(EXPRESSION, [key], True)
    assert not clusters.recall_members(store, GROUP)
    assert await build() is None
    assert store.set_removed(EXPRESSION, [key], False)
    assert _member_keys(clusters, store) == {frozenset((key,))}

    stamp = datetime(2026, 11, 24, tzinfo=UTC)
    assert store.review_expressions([key], ExpressionReviewStatus.REJECTED, "不采用", stamp) == 1
    assert _member_keys(clusters, store) == {frozenset((key,))}
    assert store.review_expressions([key], ExpressionReviewStatus.APPROVED, "改为采用", stamp) == 1
    assert _member_keys(clusters, store) == {frozenset((key,))}
    assert store.update_entry(EXPRESSION, key, LanguageContent("同名", "新场景"), None)
    assert not clusters.recall_members(store, GROUP)
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_expression_cluster_recall_keeps_source_separate_from_package_scope(
    store: LanguageStore,
) -> None:
    sample = LanguageSample("synthetic:source-message", 10, "synthetic:speaker", "不嵌入私聊原句")
    store.observe([sample], [])
    store.observe_extraction(
        EXPRESSION,
        PRIVATE,
        [sample],
        {sample.record_id: sample.content},
        [LanguageProposal(EXPRESSION, "同名", "在线情景", (sample.record_id,))],
    )
    package = LanguagePackageRow(
        LanguageContent("同名", "包内情景"), 4, "只作来源", '{"is_global":true}'
    )
    assert store.save_package(EXPRESSION, [package]) == 1
    package_key = f"package:{package.digest}"
    online_key = store.expression_candidates(GROUP)[0].entry_key
    fake = FakeExpressionOrgan(
        {_embedding_text("在线情景", "同名"): (1, 0), _embedding_text("包内情景", "同名"): (1, 1)}
    )
    pending = await build_expression_clusters(
        store, fake.embed, cluster_count=1, authorized_entry_keys=(online_key,)
    )
    assert pending is not None
    assert _member_keys(pending, store) == {frozenset((online_key,))}
    assert fake.requests[0].embedding_inputs == (_embedding_text("在线情景", "同名"),)
    assert store.package_scope(EXPRESSION, package_key, [GROUP], confirm=True) == ()
    granted = await build_expression_clusters(
        store,
        fake.embed,
        cluster_count=1,
        authorized_entry_keys=(online_key, package_key),
        conversation_key=GROUP,
    )
    assert granted is not None
    assert _member_keys(granted, store) == {frozenset((online_key, package_key))}
    [outside] = granted.recall_members(store, OTHER)[0]
    [inside] = [entry for entry in granted.recall_members(store, GROUP)[0] if entry.package_source]
    assert outside.learning_source == PRIVATE and outside.evidence_ids == (sample.record_id,)
    assert inside.package_source == "只作来源" and inside.scope_keys == (GROUP,)
    source_page, total = store.browse_page(EXPRESSION, 0, 10, conversation_key=PRIVATE)
    assert total == 1 and source_page[0].entry_key == online_key
    assert store.package_scope(EXPRESSION, package_key, [], confirm=True) == (GROUP,)
    assert _member_keys(granted, store) == {frozenset((online_key,))}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("space", "vectors"),
    [
        ("", ((1.0,), (1.0,))),
        ("fake", ((1.0,),)),
        ("fake", ((1.0,), (1.0, 2.0))),
        ("fake", ((1.0,), (0.0,))),
        ("fake", ((1.0,), (float("nan"),))),
        ("fake", ((1.0,), (float("inf"),))),
    ],
)
async def test_expression_clusters_reject_invalid_embeddings(
    store: LanguageStore, space: str, vectors: tuple[tuple[float, ...], ...]
) -> None:
    first = store.add_entry(EXPRESSION, LanguageContent("第一项", "场景甲"))
    second = store.add_entry(EXPRESSION, LanguageContent("第二项", "场景乙"))

    async def embed(_texts: tuple[str, ...]) -> tuple[str, tuple[tuple[float, ...], ...]]:
        return space, vectors

    with pytest.raises(ValueError, match="表达向量"):
        await build_expression_clusters(
            store, embed, cluster_count=2, authorized_entry_keys=(first, second)
        )


@pytest.mark.asyncio
async def test_expression_clusters_reject_zero_center_from_valid_opposite_vectors(
    store: LanguageStore,
) -> None:
    first = store.add_entry(EXPRESSION, LanguageContent("第一项", "场景甲"))
    second = store.add_entry(EXPRESSION, LanguageContent("第二项", "场景乙"))
    fake = FakeExpressionOrgan(
        {_embedding_text("场景甲", "第一项"): (1, 0), _embedding_text("场景乙", "第二项"): (-2, 0)}
    )
    authorized = (first, second)
    split = await build_expression_clusters(
        store, fake.embed, cluster_count=2, authorized_entry_keys=authorized
    )
    assert split is not None
    assert _member_keys(split, store) == {frozenset((first,)), frozenset((second,))}
    for _ in range(2):
        with pytest.raises(ValueError, match="表达簇中心"):
            await build_expression_clusters(
                store, fake.embed, cluster_count=1, authorized_entry_keys=authorized
            )
