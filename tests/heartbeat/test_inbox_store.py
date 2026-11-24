"""平台收件箱连续已读范围的持久化与旧库迁移。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event

import pytest

from aureus.common import ManualClock
from aureus.common.sqlite import open_connection
from aureus.heartbeat import inbox_store
from aureus.heartbeat.inbox_store import InboxStore
from aureus.heartbeat.perception import MessageRoute, Perception, TrustLevel, VisibilityScope


def _clock() -> ManualClock:
    return ManualClock(datetime(2026, 11, 23, tzinfo=UTC))


def _message(sequence: int, group: str = "1", *, verified: bool = True) -> Perception:
    return Perception(
        "qq",
        "sender",
        str(sequence),
        VisibilityScope.PUBLIC,
        TrustLevel.VERIFIED_CONTACT if verified else TrustLevel.UNVERIFIED,
        route=MessageRoute("receiver", "group", group, str(sequence)),
    )


def test_priority_confirmation_keeps_older_unread_and_other_conversations(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    store = InboxStore(path, _clock())
    first = store.add(_message(1, verified=False))
    other = store.add(_message(2, "2"))
    priority = store.add(_message(3))
    newest = store.add(_message(4))
    assert first and other and priority and newest

    assert [entry.key for entry in store.list_unread_conversations()] == [
        first.conversation,
        other.conversation,
    ]
    assert store.list_priority_unread(first.conversation, ("sender",), limit=1) == [priority]
    assert store.list_priority_unread(
        first.conversation, ("sender",), after_id=priority.id, limit=1
    ) == [newest]
    store.mark_read([priority.id, newest.id])
    reopened = InboxStore(path, _clock())
    assert reopened.list_unread(first.conversation) == [first, priority, newest]
    reopened.mark_read([priority.id, first.id, priority.id])
    assert reopened.list_unread(first.conversation) == [newest]
    assert reopened.list_unread(other.conversation) == [other]
    assert [entry.id for entry in reopened.list_messages(first.conversation, limit=1)] == [
        newest.id
    ]
    assert [
        entry.status for entry in reopened.list_messages(first.conversation, before_id=newest.id)
    ] == ["read", "read"]


def test_sparse_legacy_migration_preserves_unread_and_message_ids(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    store = InboxStore(path, _clock())
    first = store.add(_message(1))
    second = store.add(_message(2))
    third = store.add(_message(3))
    other = store.add(_message(4, "2"))
    assert first and second and third and other
    with closing(open_connection(path)) as database, database:
        database.execute("ALTER TABLE inbox_conversations DROP COLUMN first_read_id")
        database.execute("ALTER TABLE inbox_conversations DROP COLUMN last_read_id")
        database.execute("ALTER TABLE inbox_messages ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0")
        database.execute(
            "UPDATE inbox_messages SET is_read = 1 WHERE id IN (?, ?, ?)",
            (first.id, third.id, other.id),
        )
        database.execute(
            "CREATE INDEX inbox_messages_unread ON inbox_messages(conversation_id, is_read, id)"
        )

    reopened = InboxStore(path, _clock())
    history = reopened.list_messages(first.conversation)
    assert [(entry.id, entry.status) for entry in history] == [
        (third.id, "unread"),
        (second.id, "unread"),
        (first.id, "read"),
    ]
    assert reopened.list_unread(other.conversation) == []
    assert reopened.add(third.perception) is None
    assert InboxStore(path, _clock()).list_messages(first.conversation) == history
    with closing(open_connection(path)) as database:
        assert "is_read" not in {
            row["name"] for row in database.execute("PRAGMA table_info(inbox_messages)")
        }
        assert [
            tuple(row)
            for row in database.execute(
                "SELECT first_read_id, last_read_id FROM inbox_conversations ORDER BY id"
            )
        ] == [(first.id, first.id), (other.id, other.id)]


def test_concurrent_acknowledgment_and_duplicate_delivery(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    store = InboxStore(path, _clock())
    first = store.add(_message(1))
    assert first is not None
    barrier = Barrier(3, timeout=5)

    def confirm() -> None:
        barrier.wait()
        store.mark_read([first.id])

    def receive() -> bool:
        barrier.wait()
        return store.add(_message(2)) is not None

    with ThreadPoolExecutor(max_workers=3) as executor:
        confirmed = executor.submit(confirm)
        deliveries = [executor.submit(receive) for _ in range(2)]
        confirmed.result()
        assert sum(delivery.result() for delivery in deliveries) == 1

    reopened = InboxStore(path, _clock())
    assert [
        (entry.perception.content, entry.status)
        for entry in reopened.list_messages(first.conversation)
    ] == [("2", "unread"), ("1", "read")]


def test_read_window_marks_only_selected_route_through_entry_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InboxStore(tmp_path / "inbox.db", _clock())
    oldest = store.add(_message(1))
    other_group = store.add(_message(2, "2"))
    another = _message(3)
    assert another.route is not None
    other_account = store.add(replace(another, route=replace(another.route, account_id="other")))
    middle = store.add(_message(4))
    newest = store.add(_message(5))
    assert oldest and middle and newest and other_group and other_account

    snapshot_taken = Event()
    writer_started = Event()
    reconstruct = inbox_store._message

    def observe_snapshot(row: inbox_store.Row) -> inbox_store.InboxMessage:
        if row["id"] == middle.id:
            snapshot_taken.set()
            assert writer_started.wait(timeout=2)
        return reconstruct(row)

    def receive() -> inbox_store.InboxMessage | None:
        assert snapshot_taken.wait(timeout=2)
        writer_started.set()
        return store.add(_message(6))

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as executor:
        patch.setattr(inbox_store, "_message", observe_snapshot)
        pending = executor.submit(receive)
        assert store.read_window(oldest.conversation, limit=2) == [middle, newest]
        late = pending.result(timeout=2)

    assert late is not None
    assert store.list_unread(oldest.conversation) == [late]
    assert store.list_unread(other_group.conversation) == [other_group]
    assert store.list_unread(other_account.conversation) == [other_account]
