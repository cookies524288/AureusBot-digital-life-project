"""人物档案的测试（路线图 1.8，05 号文档）：按账号认人，新账号就是新的人，用户只有一个。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.people import PeopleDirectory, PeopleError, PersonKind

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
USER_ACCOUNTS = [("terminal", "local"), ("qq", "10000")]


@pytest.fixture
def directory(tmp_path: Path) -> Iterator[PeopleDirectory]:
    opened = PeopleDirectory(tmp_path / "aureus.db", ManualClock(NOW))
    opened.open()
    yield opened
    opened.close()


def test_the_user_is_registered_with_every_account(directory: PeopleDirectory) -> None:
    user = directory.ensure_user("阿明", USER_ACCOUNTS)

    assert user.kind is PersonKind.USER
    assert user.is_user
    assert user.name == "阿明"
    assert user.first_seen == NOW
    assert directory.accounts_of(user.id) == [("qq", "10000"), ("terminal", "local")]
    found = directory.find("qq", "10000")
    assert found is not None and found.id == user.id


def test_registering_the_user_again_does_not_duplicate(directory: PeopleDirectory) -> None:
    first = directory.ensure_user("阿明", USER_ACCOUNTS)
    second = directory.ensure_user("阿明", USER_ACCOUNTS)

    assert second.id == first.id
    assert directory.accounts_of(first.id) == [("qq", "10000"), ("terminal", "local")]


def test_a_changed_name_and_a_new_account_are_picked_up(directory: PeopleDirectory) -> None:
    first = directory.ensure_user("阿明", [("terminal", "local")])
    second = directory.ensure_user("小明", USER_ACCOUNTS)

    assert second.id == first.id
    assert second.name == "小明"
    assert len(directory.accounts_of(first.id)) == 2


def test_an_unknown_account_is_not_found(directory: PeopleDirectory) -> None:
    directory.ensure_user("阿明", USER_ACCOUNTS)

    assert directory.find("qq", "20000") is None


def test_meeting_a_new_account_creates_a_contact(directory: PeopleDirectory) -> None:
    directory.ensure_user("阿明", USER_ACCOUNTS)
    stranger = directory.meet("qq", "20000")

    assert stranger.kind is PersonKind.CONTACT
    assert not stranger.is_user
    assert stranger.name == "QQ 20000"
    assert directory.accounts_of(stranger.id) == [("qq", "20000")]


def test_a_new_contact_uses_the_incoming_display_name(directory: PeopleDirectory) -> None:
    stranger = directory.meet("qq", "20000", "小甲")

    assert stranger.name == "小甲"


def test_a_new_contact_without_a_display_name_uses_platform_and_account(
    directory: PeopleDirectory,
) -> None:
    stranger = directory.meet("qq", "20000", "  ")

    assert stranger.name == "QQ 20000"


def test_an_unknown_platform_uses_only_the_account(directory: PeopleDirectory) -> None:
    stranger = directory.meet("", "20000")

    assert stranger.name == "20000"


def test_a_later_display_name_replaces_the_temporary_name(
    directory: PeopleDirectory,
) -> None:
    first = directory.meet("qq", "20000")
    second = directory.meet("qq", "20000", "小甲")

    assert second.id == first.id
    assert second.name == "小甲"


def test_a_later_display_name_fills_an_empty_archive(tmp_path: Path) -> None:
    path = tmp_path / "aureus.db"
    directory = PeopleDirectory(path, ManualClock(NOW))
    directory.open()
    try:
        stranger = directory.meet("qq", "20000")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE people SET name = '' WHERE id = ?", (stranger.id,))

        renamed = directory.meet("qq", "20000", "小甲")

        assert renamed.name == "小甲"
    finally:
        directory.close()


def test_a_later_display_name_does_not_overwrite_a_real_name(
    directory: PeopleDirectory,
) -> None:
    directory.meet("qq", "20000", "小甲")

    renamed = directory.meet("qq", "20000", "小乙")

    assert renamed.name == "小甲"


def test_meeting_the_same_account_returns_the_same_person(directory: PeopleDirectory) -> None:
    first = directory.meet("qq", "20000")
    second = directory.meet("qq", "20000")

    assert second.id == first.id


def test_the_same_id_on_another_platform_is_another_person(directory: PeopleDirectory) -> None:
    on_qq = directory.meet("qq", "20000")
    on_wechat = directory.meet("wechat", "20000")

    assert on_wechat.id != on_qq.id


def test_meeting_the_users_account_gives_the_user(directory: PeopleDirectory) -> None:
    user = directory.ensure_user("阿明", USER_ACCOUNTS)
    met = directory.meet("qq", "10000")

    assert met.id == user.id
    assert met.is_user


def test_an_account_owned_by_a_contact_cannot_become_the_users(
    directory: PeopleDirectory,
) -> None:
    directory.meet("qq", "10000")

    with pytest.raises(PeopleError, match="已经登记在别人名下"):
        directory.ensure_user("阿明", USER_ACCOUNTS)


def test_a_failed_registration_leaves_nothing_behind(directory: PeopleDirectory) -> None:
    directory.meet("qq", "10000")

    with pytest.raises(PeopleError):
        directory.ensure_user("阿明", USER_ACCOUNTS)

    assert directory.find("terminal", "local") is None


def test_the_profiles_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "aureus.db"
    first = PeopleDirectory(path, ManualClock(NOW))
    first.open()
    stranger = first.meet("qq", "20000")
    first.close()

    second = PeopleDirectory(path, ManualClock(NOW))
    second.open()
    try:
        found = second.find("qq", "20000")
        assert found is not None and found.id == stranger.id
    finally:
        second.close()


def test_using_it_before_opening_is_an_error(tmp_path: Path) -> None:
    closed = PeopleDirectory(tmp_path / "aureus.db", ManualClock(NOW))

    with pytest.raises(RuntimeError, match="还没有打开"):
        closed.find("qq", "1")
