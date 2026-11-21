"""身份版本历史的测试（路线图 1.9，D62）。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureus.common import ManualClock
from aureus.identity import (
    Identity,
    IdentityHistory,
    IdentityHistoryError,
    IdentityLayer,
    VersionSource,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def make_identity(
    *,
    accounts: list[dict[str, str]] | None = None,
    locks: dict[str, bool] | None = None,
    **persona: str,
) -> Identity:
    data: dict[str, Any] = {
        "facts": {"name": "小绿", "birthday": "2008-01-01"},
        "user": {"name": "阿明", "accounts": accounts or []},
        "persona": {"immutable": "温和。", "expression_style": "句子短。", **persona},
    }
    if locks is not None:
        data["persona"]["locks"] = locks
    return Identity.model_validate(data)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def history(tmp_path: Path, clock: ManualClock) -> Iterator[IdentityHistory]:
    opened = IdentityHistory(tmp_path / "aureus.db", clock)
    opened.open()
    yield opened
    opened.close()


def sources(history: IdentityHistory, layer: IdentityLayer) -> list[VersionSource]:
    return [version.source for version in history.versions(layer, limit=100)]


def test_the_first_start_records_initial_versions_and_no_user_change(
    history: IdentityHistory,
) -> None:
    effective = history.sync_from_file(make_identity())

    assert effective == make_identity()
    for layer in IdentityLayer:
        assert sources(history, layer) == [VersionSource.INITIAL]
    assert history.user_changes_after(0) == []


def test_an_unchanged_file_adds_nothing(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity())
    history.sync_from_file(make_identity())

    assert len(history.versions(limit=100)) == len(IdentityLayer)


def test_get_and_latest_self_change_skip_user_versions(history: IdentityHistory) -> None:
    layer = IdentityLayer.EXPRESSION_STYLE
    history.sync_from_file(make_identity())
    assert history.get(999) is None
    assert history.latest_self_change(layer) is None
    history.record_self_change(layer, "句子短，多关心对方。")
    changed = history.versions(layer, limit=1)[0]
    history.sync_from_file(make_identity(expression_style="用户改过。"))
    assert history.get(changed.id) == changed
    assert history.latest_self_change(layer) == changed
    assert history.latest_self_change(IdentityLayer.MUTABLE) is None


def test_explicit_edit_restores_original_without_duplicate_versions(
    history: IdentityHistory,
) -> None:
    layer = IdentityLayer.EXPRESSION_STYLE
    original = make_identity()
    history.sync_from_file(original)
    history.record_self_change(layer, "句子短，多关心对方。")
    restored = history.sync_from_file(original, edited=[layer])
    assert restored.persona.expression_style == "句子短。"
    assert sources(history, layer) == [
        VersionSource.INITIAL,
        VersionSource.SELF,
        VersionSource.USER_FILE,
    ]
    history.sync_from_file(original, edited=[layer, layer])
    assert len(history.user_changes_after(0)) == 1
    history.sync_from_file(make_identity(expression_style="新原文。"), edited=[layer])
    assert len(history.user_changes_after(0)) == 2


def test_a_changed_layer_becomes_a_user_change(
    history: IdentityHistory, clock: ManualClock
) -> None:
    history.sync_from_file(make_identity())
    clock.advance(timedelta(days=1))
    effective = history.sync_from_file(make_identity(expression_style="多用语气词。"))

    assert effective.persona.expression_style == "多用语气词。"
    changes = history.user_changes_after(0)
    assert [(c.layer, c.content) for c in changes] == [
        (IdentityLayer.EXPRESSION_STYLE, "多用语气词。")
    ]
    assert changes[0].created_at == NOW + timedelta(days=1)
    # 旧版本还在。
    old = history.versions(IdentityLayer.EXPRESSION_STYLE, limit=100)
    assert [v.content for v in old] == ["句子短。", "多用语气词。"]


def test_whitespace_alone_is_not_a_change(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity())
    history.sync_from_file(make_identity(expression_style="\n  句子短。 \n"))

    assert history.user_changes_after(0) == []


def test_clearing_a_layer_is_a_change(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity())
    effective = history.sync_from_file(make_identity(expression_style=""))

    assert effective.persona.expression_style == ""
    assert [c.layer for c in history.user_changes_after(0)] == [IdentityLayer.EXPRESSION_STYLE]


def test_changed_facts_and_immutable_persona_are_recorded(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity())
    changed = make_identity(immutable="温和，倔强。")
    changed = changed.model_copy(
        update={"facts": changed.facts.model_copy(update={"name": "小青"})}
    )
    history.sync_from_file(changed)

    assert {c.layer for c in history.user_changes_after(0)} == {
        IdentityLayer.FACTS,
        IdentityLayer.IMMUTABLE,
    }


def test_the_users_new_account_is_a_change_of_facts(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity())
    history.sync_from_file(make_identity(accounts=[{"platform": "qq", "id": "1"}]))

    assert [c.layer for c in history.user_changes_after(0)] == [IdentityLayer.FACTS]


def test_reordering_accounts_is_not_a_change(history: IdentityHistory) -> None:
    first = [{"platform": "qq", "id": "1"}, {"platform": "wechat", "id": "2"}]
    history.sync_from_file(make_identity(accounts=first))
    history.sync_from_file(make_identity(accounts=list(reversed(first))))

    assert history.user_changes_after(0) == []


def test_its_own_change_survives_a_restart_with_an_unchanged_file(
    history: IdentityHistory,
) -> None:
    history.sync_from_file(make_identity())
    history.record_self_change(IdentityLayer.MUTABLE, "想变得更耐心。")

    effective = history.sync_from_file(make_identity())

    assert effective.persona.mutable == "想变得更耐心。"
    assert history.user_changes_after(0) == []


def test_a_user_edit_wins_over_its_own_change(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity())
    history.record_self_change(IdentityLayer.MUTABLE, "想变得更耐心。")

    effective = history.sync_from_file(make_identity(mutable="想多交朋友。"))

    assert effective.persona.mutable == "想多交朋友。"
    assert sources(history, IdentityLayer.MUTABLE) == [
        VersionSource.INITIAL,
        VersionSource.SELF,
        VersionSource.USER_FILE,
    ]


def test_a_locked_layer_cannot_be_changed_by_itself(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity(locks={"expression_style": True}))

    with pytest.raises(IdentityHistoryError, match="被用户锁定了"):
        history.record_self_change(IdentityLayer.EXPRESSION_STYLE, "随便说。")
    assert sources(history, IdentityLayer.EXPRESSION_STYLE) == [VersionSource.INITIAL]


def test_the_user_can_still_edit_a_locked_layer(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity(locks={"expression_style": True}))
    effective = history.sync_from_file(
        make_identity(locks={"expression_style": True}, expression_style="慢慢说。")
    )

    assert effective.persona.expression_style == "慢慢说。"


@pytest.mark.parametrize("layer", [IdentityLayer.FACTS, IdentityLayer.IMMUTABLE])
def test_facts_and_the_immutable_persona_are_only_for_the_user(
    history: IdentityHistory, layer: IdentityLayer
) -> None:
    history.sync_from_file(make_identity())

    with pytest.raises(IdentityHistoryError, match="只有用户能改"):
        history.record_self_change(layer, "改一下")


def test_no_self_change_before_the_locks_are_known(history: IdentityHistory) -> None:
    with pytest.raises(IdentityHistoryError, match="还不知道哪些层被锁定"):
        history.record_self_change(IdentityLayer.MUTABLE, "想变得更耐心。")


def test_user_changes_after_a_version_skips_older_ones(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity())
    history.sync_from_file(make_identity(expression_style="第一次改。"))
    first = history.user_changes_after(0)[-1].id
    history.sync_from_file(make_identity(expression_style="第二次改。"))

    assert [c.content for c in history.user_changes_after(first)] == ["第二次改。"]


def test_versions_are_limited_to_the_latest(history: IdentityHistory) -> None:
    history.sync_from_file(make_identity())
    for index in range(4):
        history.sync_from_file(make_identity(expression_style=f"第{index}版。"))

    latest = history.versions(IdentityLayer.EXPRESSION_STYLE, limit=2)
    assert [v.content for v in latest] == ["第2版。", "第3版。"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE identity_versions SET content = 'x'",
        "DELETE FROM identity_versions",
        "INSERT OR REPLACE INTO identity_versions (id, layer, content, source, created_at) "
        "VALUES (1, 'mutable', 'x', 'self', '2026-01-01')",
    ],
)
def test_versions_cannot_be_rewritten(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "aureus.db"
    history = IdentityHistory(path, ManualClock(NOW))
    history.open()
    history.sync_from_file(make_identity())
    history.close()

    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(statement)
    finally:
        conn.close()


def test_using_it_before_opening_is_an_error(tmp_path: Path) -> None:
    closed = IdentityHistory(tmp_path / "aureus.db", ManualClock(NOW))

    with pytest.raises(RuntimeError, match="还没有打开"):
        closed.sync_from_file(make_identity())
