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


def test_list_mentioned_unread_filters_verified_unread_and_paginates(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", _clock())
    first = store.add(replace(_message(1), mentioned=True))
    unmentioned = store.add(_message(2))
    unverified = store.add(replace(_message(3, verified=False), mentioned=True))
    later = store.add(replace(_message(4), mentioned=True))
    other_group = store.add(replace(_message(5, "2"), mentioned=True))
    assert first and unmentioned and unverified and later and other_group

    conversation = first.conversation
    assert store.list_mentioned_unread(conversation, limit=1) == [first]
    assert store.list_mentioned_unread(conversation, after_id=first.id, limit=1) == [later]
    assert store.list_mentioned_unread(conversation, after_id=later.id) == []
    store.mark_read([first.id])
    assert store.list_mentioned_unread(conversation) == [later]


def test_list_attention_unread_combines_senders_mentions_and_paginates(tmp_path: Path) -> None:
    """D137：绑定用户/特别关心排在仅提及之前，两档都按入箱序号分页。"""
    store = InboxStore(tmp_path / "inbox.db", _clock())

    def save(
        number: int,
        *,
        sender: str = "sender",
        mentioned: bool = False,
        group: str = "1",
        verified: bool = True,
    ):
        entry = store.add(
            replace(
                _message(number, group, verified=verified),
                sender_id=sender,
                mentioned=mentioned,
            )
        )
        assert entry is not None
        return entry

    mention_old = save(1, mentioned=True)
    owner = save(2, sender="owner")
    mention_later = save(3, mentioned=True)
    unverified = save(4, mentioned=True, sender="stranger", verified=False)
    owner_mentioned = save(5, sender="owner", mentioned=True)
    ordinary = save(6)
    other_group = save(7, sender="owner", mentioned=True, group="2")
    conversation = owner.conversation

    assert store.list_attention_unread(conversation, ["owner"]) == [
        owner,
        owner_mentioned,
        mention_old,
        mention_later,
    ]
    first = store.list_attention_unread(conversation, ["owner"], limit=2)
    assert first == [owner, owner_mentioned]
    assert store.list_attention_unread(
        conversation, ["owner"], after=(0, owner_mentioned.id), limit=2
    ) == [mention_old, mention_later]
    assert store.list_attention_unread(conversation, ["owner"], after=(0, owner.id), limit=2) == [
        owner_mentioned,
        mention_old,
    ]
    assert store.list_attention_unread(conversation, []) == [
        mention_old,
        mention_later,
        owner_mentioned,
    ]
    assert unverified.id not in {row.id for row in first}
    assert ordinary.id not in {row.id for row in first}
    assert other_group.id not in {row.id for row in first}


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
        assert store.read_window(oldest.conversation, limit=2) == [oldest, middle]
        late = pending.result(timeout=2)

    assert late is not None
    assert store.list_unread(oldest.conversation) == [newest, late]
    assert store.list_unread(other_group.conversation) == [other_group]
    assert store.list_unread(other_account.conversation) == [other_account]


def test_read_window_does_not_skip_unread_when_limit_smaller(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", _clock())
    saved = []
    for number in range(1, 6):
        entry = store.add(_message(number))
        assert entry is not None
        saved.append(entry)
    conversation = saved[0].conversation

    assert [entry.perception.content for entry in store.read_window(conversation, limit=2)] == [
        "1",
        "2",
    ]
    assert [entry.perception.content for entry in store.list_unread(conversation)] == [
        "3",
        "4",
        "5",
    ]
    assert [entry.perception.content for entry in store.read_window(conversation, limit=2)] == [
        "3",
        "4",
    ]
    assert [entry.perception.content for entry in store.list_unread(conversation)] == ["5"]


def test_read_latest_and_clear_returns_newest_batch_and_drops_older_unread(tmp_path: Path) -> None:
    """D145：只读最新一批，返回按时间升序，更早未读一并标已读。"""
    path = tmp_path / "inbox.db"
    store = InboxStore(path, _clock())
    saved = []
    for number in range(1, 7):
        entry = store.add(_message(number))
        assert entry is not None
        saved.append(entry)
    other = store.add(_message(7, "2"))
    assert other is not None
    conversation = saved[0].conversation

    latest = store.read_latest_and_clear(conversation, limit=2)

    assert [entry.perception.content for entry in latest] == ["5", "6"]
    assert [entry.perception.content for entry in store.list_unread(conversation)] == []
    assert [entry.perception.content for entry in store.list_unread(other.conversation)] == ["7"]
    reopened = InboxStore(path, _clock())
    assert [entry.status for entry in reopened.list_messages(conversation)] == [
        "read",
        "read",
        "read",
        "read",
        "read",
        "read",
    ]
    with closing(open_connection(path)) as database:
        [row] = database.execute(
            "SELECT first_read_id, last_read_id FROM inbox_conversations WHERE recipient_id = '1'"
        )
    assert tuple(row) == (saved[0].id, saved[-1].id)


def test_read_latest_and_clear_without_unread_keeps_the_watermark(tmp_path: Path) -> None:
    """没有未读时返回空、不改水位；已读消息不会重新变成未读。"""
    path = tmp_path / "inbox.db"
    store = InboxStore(path, _clock())
    first = store.add(_message(1))
    assert first is not None
    store.mark_read([first.id])
    with closing(open_connection(path)) as database:
        [before] = database.execute("SELECT first_read_id, last_read_id FROM inbox_conversations")

    assert store.read_latest_and_clear(first.conversation, limit=5) == []

    with closing(open_connection(path)) as database:
        [after] = database.execute("SELECT first_read_id, last_read_id FROM inbox_conversations")
    assert tuple(after) == tuple(before)
    assert store.list_unread(first.conversation) == []
    second = store.add(_message(2))
    assert second is not None
    assert store.list_unread(first.conversation) == [second]


def test_read_latest_and_clear_limit_larger_than_unread_returns_all(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", _clock())
    saved = []
    for number in range(1, 4):
        entry = store.add(_message(number))
        assert entry is not None
        saved.append(entry)
    conversation = saved[0].conversation

    assert [
        entry.perception.content for entry in store.read_latest_and_clear(conversation, limit=10)
    ] == ["1", "2", "3"]
    assert store.list_unread(conversation) == []
    with closing(open_connection(tmp_path / "inbox.db")) as database:
        [row] = database.execute(
            "SELECT last_read_id FROM inbox_conversations WHERE recipient_id = '1'"
        )
    assert row[0] == saved[-1].id


def test_read_latest_and_clear_keeps_skipped_messages_in_read_history(tmp_path: Path) -> None:
    """被跳过标已读的消息仍在收件箱与已读历史里，可按关键字查回（D145 第四条）。"""
    store = InboxStore(tmp_path / "inbox.db", _clock())
    saved = []
    for number in range(1, 6):
        entry = store.add(_message(number))
        assert entry is not None
        saved.append(entry)
    conversation = saved[0].conversation

    assert [
        entry.perception.content for entry in store.read_latest_and_clear(conversation, limit=1)
    ] == ["5"]
    assert store.list_unread(conversation) == []
    history = store.list_read_history(
        conversation,
        VisibilityScope.PUBLIC,
        before_id=None,
        keyword=None,
        limit=10,
    )
    assert [entry.perception.content for entry in history] == ["5", "4", "3", "2", "1"]
    skipped = store.list_read_history(
        conversation,
        VisibilityScope.PUBLIC,
        before_id=None,
        keyword="2",
        limit=10,
    )
    assert [entry.perception.content for entry in skipped] == ["2"]
    assert [entry.status for entry in store.list_messages(conversation)] == [
        "read",
        "read",
        "read",
        "read",
        "read",
    ]


def test_read_latest_and_clear_only_advances_to_the_newest_incoming(tmp_path: Path) -> None:
    """水位以最大入站消息为准；之后的外发消息不会让新来信变成未读。"""
    store = InboxStore(tmp_path / "inbox.db", _clock())
    first = store.add(_message(1))
    second = store.add(_message(2))
    assert first and second
    conversation = first.conversation

    assert [entry.id for entry in store.read_latest_and_clear(conversation, limit=10)] == [
        first.id,
        second.id,
    ]
    store.add_delivered(_message(3), sender_name="bot")
    assert store.list_unread(conversation) == []
    third = store.add(_message(4))
    assert third is not None
    assert store.list_unread(conversation) == [third]


def test_find_platform_message_matches_complete_route_without_reading(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", _clock())
    incoming = store.add(_message(1, "1"))
    other_group = store.add(_message(1, "2"))
    assert incoming and other_group

    assert store.find_platform_message(incoming.conversation, "1") == incoming
    assert store.find_platform_message(incoming.conversation, "2") is None
    assert store.list_unread(incoming.conversation) == [incoming]

    store.add_delivered(_message(3, "1"), sender_name="bot")
    outgoing = store.find_platform_message(incoming.conversation, "3")
    assert outgoing is not None and outgoing.direction == "outgoing"

    store.archive(
        replace(_message(4, "1"), content="平台事件"),
        sender_name="system",
        conversation_name="群 1",
        notice=True,
    )
    assert store.find_platform_message(incoming.conversation, "4") is None


def test_delivered_replies_only_follows_own_remaining_direct_segments(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", _clock())
    source = store.add(_message(1))
    assert source is not None
    for number, group, sender, origin in (
        (2, "1", "receiver", "1"),
        (3, "1", "receiver", "1"),
        (4, "1", "receiver", None),
        (5, "1", "sender", "1"),
        (6, "2", "receiver", "1"),
        (7, "1", "receiver", "9"),
    ):
        store.add_delivered(
            replace(_message(number, group), sender_id=sender, source_message_id=origin),
            sender_name="bot",
        )
    store.mark_recalled(source.conversation, "3")
    assert [
        row.perception.content for row in store.delivered_replies(source.conversation, "1")
    ] == ["2"]


def test_mark_recalled_hides_existing_and_future_platform_original(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    store = InboxStore(path, _clock())
    message = store.add(_message(1))
    assert message is not None

    store.mark_recalled(message.conversation, "1")
    store.mark_recalled(message.conversation, "1")
    assert store.find_platform_message(message.conversation, "1") is None
    [retained] = store.list_messages(message.conversation)
    assert retained.direction == "recalled" and retained.id == message.id
    assert store.list_unread(message.conversation) == []

    store.mark_recalled(message.conversation, "2")
    assert store.add(_message(2)) is None
    assert store.find_platform_message(message.conversation, "2") is None


def test_mark_read_after_clearing_is_idempotent_and_keeps_the_watermark(tmp_path: Path) -> None:
    """D145：清空后再对已读 id 走 mark_read，不报错、不把水位拉回。"""
    path = tmp_path / "inbox.db"
    store = InboxStore(path, _clock())
    first = store.add(_message(1))
    second = store.add(_message(2))
    assert first and second
    conversation = first.conversation
    assert [entry.id for entry in store.read_latest_and_clear(conversation, limit=1)] == [second.id]

    store.mark_read([first.id, second.id])
    with closing(open_connection(path)) as database:
        [row] = database.execute("SELECT last_read_id FROM inbox_conversations")
    assert row[0] == second.id
    assert store.list_unread(conversation) == []

    later = store.add(_message(3))
    assert later is not None
    store.mark_read([first.id, second.id])
    with closing(open_connection(path)) as database:
        [row] = database.execute("SELECT last_read_id FROM inbox_conversations")
    assert row[0] == second.id
    assert store.list_unread(conversation) == [later]
