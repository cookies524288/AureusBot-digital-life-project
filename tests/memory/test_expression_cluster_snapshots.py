"""用合成表达验收聚类快照的持久化和回滚。"""

import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest

from aureus.memory import LanguageContent, LanguageKind, LanguageStore
from aureus.memory.expression_clusters import (
    ExpressionClusters,
    ExpressionClusterStore,
    build_expression_clusters,
)
from aureus.memory.language import LanguagePackageRow

EXPRESSION = LanguageKind.EXPRESSION
FIRST_SPACE = "synthetic:first"
SECOND_SPACE = "synthetic:second"


@pytest.fixture
def language(tmp_path: Path) -> LanguageStore:
    store = LanguageStore(tmp_path / "synthetic-language.sqlite")
    store.open()
    return store


@pytest.fixture
def snapshot_path(tmp_path: Path) -> Path:
    return tmp_path / "synthetic-expression-clusters.sqlite"


async def _build(
    language: LanguageStore,
    space: str,
    *keys: str,
    cluster_count: int = 1,
    conversation_key: str | None = None,
) -> ExpressionClusters:
    async def embed(texts: tuple[str, ...]) -> tuple[str, tuple[tuple[float, ...], ...]]:
        return space, ((1.25, 2.5),) * len(texts)

    snapshot = await build_expression_clusters(
        language,
        embed,
        authorized_entry_keys=keys,
        cluster_count=cluster_count,
        conversation_key=conversation_key,
    )
    assert snapshot is not None
    return snapshot


def _add(language: LanguageStore, text: str) -> str:
    return language.add_entry(EXPRESSION, LanguageContent(text, "合成情景", "合成例句"))


@pytest.mark.asyncio
async def test_snapshot_reopens_with_exact_cluster_and_member_order(
    language: LanguageStore, snapshot_path: Path
) -> None:
    keys = [_add(language, text) for text in ("说法甲", "说法乙", "说法丙")]
    snapshot = await _build(language, FIRST_SPACE, *keys, cluster_count=2)
    store = ExpressionClusterStore(snapshot_path)
    assert store.load(FIRST_SPACE) is None
    store.save(snapshot)

    reopened = ExpressionClusterStore(snapshot_path)
    assert reopened.load(FIRST_SPACE) == snapshot
    assert reopened.load("Synthetic:first") is None


@pytest.mark.asyncio
async def test_snapshot_replacement_is_isolated_by_embedding_space(
    language: LanguageStore, snapshot_path: Path
) -> None:
    first = _add(language, "空间甲")
    second = _add(language, "空间乙")
    replacement = _add(language, "空间甲替换")
    before = await _build(language, FIRST_SPACE, first, replacement, cluster_count=2)
    other = await _build(language, SECOND_SPACE, second)
    after = await _build(language, FIRST_SPACE, replacement)
    store = ExpressionClusterStore(snapshot_path)
    store.save(before)
    store.save(other)
    assert store.load(FIRST_SPACE) == before
    assert store.load(SECOND_SPACE) == other

    store.save(after)
    assert store.load(FIRST_SPACE) == after
    assert store.load(SECOND_SPACE) == other
    with closing(sqlite3.connect(snapshot_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM expression_cluster_snapshots"
        ).fetchone() == (2,)
        assert {
            tuple(row)
            for row in connection.execute(
                "SELECT cluster_id, entry_key FROM expression_cluster_members"
            )
        } == {(0, replacement), (0, second)}


@pytest.mark.asyncio
async def test_snapshot_write_failure_rolls_back_removal_and_partial_insert(
    language: LanguageStore, snapshot_path: Path
) -> None:
    old_keys = [_add(language, text) for text in ("旧说法甲", "旧说法乙")]
    new_keys = [_add(language, text) for text in ("新说法甲", "新说法乙")]
    before = await _build(language, FIRST_SPACE, *old_keys, cluster_count=2)
    after = await _build(language, FIRST_SPACE, *new_keys)
    store = ExpressionClusterStore(snapshot_path)
    store.save(before)

    with closing(sqlite3.connect(snapshot_path)) as connection, connection:
        [old_id] = connection.execute(
            "SELECT snapshot_id FROM expression_cluster_snapshots"
        ).fetchone()
        connection.execute(
            """
            CREATE TRIGGER reject_second_member BEFORE INSERT ON expression_cluster_members
            WHEN NEW.member_order = 1
            BEGIN
                SELECT CASE
                    WHEN EXISTS (SELECT 1 FROM expression_cluster_members
                        WHERE snapshot_id <> NEW.snapshot_id)
                    THEN RAISE(ABORT, 'old members still present')
                    WHEN NOT EXISTS (SELECT 1 FROM expression_cluster_members
                        WHERE snapshot_id = NEW.snapshot_id AND member_order = 0)
                    THEN RAISE(ABORT, 'first member not inserted')
                    ELSE RAISE(ABORT, 'synthetic member failure')
                END;
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic member failure"):
        store.save(after)
    assert ExpressionClusterStore(snapshot_path).load(FIRST_SPACE) == before
    with closing(sqlite3.connect(snapshot_path)) as connection:
        assert connection.execute(
            "SELECT snapshot_id FROM expression_cluster_snapshots"
        ).fetchall() == [(old_id,)]
        assert connection.execute(
            "SELECT DISTINCT snapshot_id FROM expression_cluster_members"
        ).fetchall() == [(old_id,)]


@pytest.mark.asyncio
async def test_snapshot_contains_only_derived_references_and_rechecks_live_content(
    language: LanguageStore, snapshot_path: Path
) -> None:
    key = _add(language, "合成风格")
    snapshot = await _build(language, FIRST_SPACE, key)
    store = ExpressionClusterStore(snapshot_path)
    store.save(snapshot)
    with closing(sqlite3.connect(snapshot_path)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall() == [
            ("expression_cluster_snapshots",),
            ("expression_cluster_members",),
        ]
        headers = connection.execute("SELECT * FROM expression_cluster_snapshots")
        assert [column[0] for column in headers.description] == [
            "snapshot_id",
            "embedding_space",
            "format_version",
            "cluster_count",
        ]
        [snapshot_id, space, version, count] = headers.fetchone()
        assert snapshot_id and (space, version, count) == (FIRST_SPACE, 1, 1)
        members = connection.execute("SELECT * FROM expression_cluster_members")
        assert [column[0] for column in members.description] == [
            "snapshot_id",
            "cluster_id",
            "member_order",
            "entry_key",
            "fingerprint",
        ]
        fingerprint = sha256("情景：合成情景\n风格：合成风格".encode()).hexdigest()
        assert members.fetchall() == [(snapshot_id, 0, 0, key, fingerprint)]

    loaded = store.load(FIRST_SPACE)
    assert loaded is not None
    assert [entry.entry_key for entry in loaded.recall_members(language, None)[0]] == [key]
    assert language.update_entry(EXPRESSION, key, LanguageContent("合成风格", "新版情景"), None)
    assert not loaded.recall_members(language, None)


@pytest.mark.asyncio
async def test_loaded_snapshot_does_not_override_package_scope(
    language: LanguageStore, snapshot_path: Path
) -> None:
    conversation = "synthetic:group"
    package = LanguagePackageRow(LanguageContent("包表达", "包内情景"), 1, "合成来源", "{}")
    assert language.save_package(EXPRESSION, [package]) == 1
    key = f"package:{package.digest}"
    assert language.package_scope(EXPRESSION, key, [conversation], confirm=True) == ()
    snapshot = await _build(language, FIRST_SPACE, key, conversation_key=conversation)
    store = ExpressionClusterStore(snapshot_path)
    store.save(snapshot)

    loaded = store.load(FIRST_SPACE)
    assert loaded is not None
    assert [entry.entry_key for entry in loaded.recall_members(language, conversation)[0]] == [key]
    assert not loaded.recall_members(language, "synthetic:other")
    assert language.package_scope(EXPRESSION, key, [], confirm=True) == (conversation,)
    assert not loaded.recall_members(language, conversation)


@pytest.mark.asyncio
async def test_snapshot_rejects_unknown_format_version(
    language: LanguageStore, snapshot_path: Path
) -> None:
    key = _add(language, "合成说法")
    snapshot = await _build(language, FIRST_SPACE, key)
    store = ExpressionClusterStore(snapshot_path)
    store.save(snapshot)
    with closing(sqlite3.connect(snapshot_path)) as connection, connection:
        connection.execute("UPDATE expression_cluster_snapshots SET format_version = 999")

    with pytest.raises(ValueError, match="快照格式版本"):
        store.load(FIRST_SPACE)
