from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.people import LINK_COMMAND, LINK_LIFETIME, AccountLink, PeopleDirectory, PeopleError


def test_account_link_requires_both_sides_and_persists(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 22, tzinfo=UTC))
    path = tmp_path / "test.db"
    directory = PeopleDirectory(path, clock)
    directory.open()
    web = ("web", "local")
    user = directory.ensure_user("用户", [web])
    link = AccountLink(directory, clock, web, "qq", "9000")

    with pytest.raises(PeopleError):
        link.issue("session", "9000")
    code = link.issue("session", "2000")
    command = f"{LINK_COMMAND} {code}"
    for sender, text, direct in [
        ("2001", command, True),
        ("2000", command, False),
        ("2000", f"{LINK_COMMAND} 错误", True),
    ]:
        assert link.receive(sender, text, is_direct=direct)
        with pytest.raises(PeopleError):
            link.confirm("session")
        assert directory.find("qq", "2000") is None

    assert link.receive("2000", command, is_direct=True)
    with pytest.raises(PeopleError):
        link.confirm("another-session")
    assert link.confirm("session") == "2000"
    assert link.receive("2000", command, is_direct=True)
    with pytest.raises(PeopleError):
        link.confirm("session")
    directory.close()

    directory.open()
    try:
        assert directory.find("qq", "2000") == user
        assert directory.find("qq", "2001") is None
    finally:
        directory.close()


def test_account_link_expiry_revocation_and_conflict(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 22, tzinfo=UTC))
    directory = PeopleDirectory(tmp_path / "test.db", clock)
    directory.open()
    try:
        web = ("web", "local")
        directory.ensure_user("用户", [web])
        link = AccountLink(directory, clock, web, "qq", "9000")
        code = link.issue("session", "2000")
        link.receive("2000", f"{LINK_COMMAND} {code}", is_direct=True)
        clock.advance(LINK_LIFETIME)
        with pytest.raises(PeopleError):
            link.confirm("session")
        code = link.issue("session", "2000")
        link.revoke("session")
        link.receive("2000", f"{LINK_COMMAND} {code}", is_direct=True)
        with pytest.raises(PeopleError):
            link.confirm("session")

        code = link.issue("session", "2000")
        contact = directory.meet("qq", "2000")
        link.receive("2000", f"{LINK_COMMAND} {code}", is_direct=True)
        with pytest.raises(PeopleError):
            link.confirm("session")
        with pytest.raises(PeopleError):
            link.issue("session", "2000")
        assert directory.find("qq", "2000") == contact
    finally:
        directory.close()
