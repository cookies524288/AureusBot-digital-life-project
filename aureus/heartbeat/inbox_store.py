"""持久收件箱：按会话保留收到的感知和阅读状态。"""

import json
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Row

from aureus.common import Clock
from aureus.common.sqlite import open_connection

from .perception import MediaReference, MessageRoute, Perception, TrustLevel, VisibilityScope

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_conversations (
    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, account_id TEXT NOT NULL,
    kind TEXT NOT NULL, recipient_id TEXT NOT NULL, temporary_group_id TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (platform, account_id, kind, recipient_id, temporary_group_id)
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES inbox_conversations(id),
    message_id TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
    content TEXT NOT NULL, visibility TEXT NOT NULL, trust TEXT NOT NULL,
    mentioned INTEGER NOT NULL, quoted_message_id TEXT, source_timestamp INTEGER,
    source_message_id TEXT, timestamp TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0,
    attachments TEXT NOT NULL DEFAULT '[]',
    UNIQUE (conversation_id, message_id)
);
CREATE INDEX IF NOT EXISTS inbox_messages_unread
ON inbox_messages (conversation_id, is_read, id);
"""

_KEY = (
    "platform = ? AND account_id = ? AND kind = ? AND recipient_id = ? AND temporary_group_id = ?"
)
_MESSAGES = """
SELECT m.*, c.platform, c.account_id, c.kind, c.recipient_id, c.temporary_group_id, c.name
FROM inbox_messages m JOIN inbox_conversations c ON c.id = m.conversation_id
"""


@dataclass(frozen=True)
class ConversationKey:
    """平台账号下的群聊、普通私聊或临时私聊。"""

    platform: str
    account_id: str
    kind: str
    recipient_id: str
    temporary_group_id: str | None = None

    @classmethod
    def from_perception(cls, perception: Perception) -> "ConversationKey":
        """从已通过通道范围检查的感知提取会话。"""
        route = perception.route
        if route is None:
            raise ValueError("收件箱感知必须有回投地址")
        return cls(
            perception.channel,
            route.account_id,
            route.kind,
            route.recipient_id,
            route.temporary_group_id,
        )

    def parameters(self) -> tuple[str, ...]:
        """SQLite 使用空串表示没有临时群号，保证唯一键可比较。"""
        return (
            self.platform,
            self.account_id,
            self.kind,
            self.recipient_id,
            self.temporary_group_id or "",
        )


@dataclass(frozen=True)
class InboxConversation:
    """会话列表中的名称、未读数量和最新消息。"""

    key: ConversationKey
    name: str
    unread_count: int
    last_message_id: int
    last_timestamp: datetime


@dataclass(frozen=True)
class InboxMessage:
    """保存后可重建原始感知的收件箱条目。"""

    id: int
    conversation: ConversationKey
    perception: Perception
    sender_name: str
    conversation_name: str
    timestamp: datetime
    status: str


def _conversation(row: Row) -> ConversationKey:
    return ConversationKey(
        row["platform"],
        row["account_id"],
        row["kind"],
        row["recipient_id"],
        row["temporary_group_id"] or None,
    )


def _message(row: Row) -> InboxMessage:
    key = _conversation(row)
    perception = Perception(
        key.platform,
        row["sender_id"],
        row["content"],
        VisibilityScope(row["visibility"]),
        TrustLevel(row["trust"]),
        route=MessageRoute(
            key.account_id, key.kind, key.recipient_id, row["message_id"], key.temporary_group_id
        ),
        mentioned=bool(row["mentioned"]),
        quoted_message_id=row["quoted_message_id"],
        source_timestamp=row["source_timestamp"],
        source_message_id=row["source_message_id"],
        attachments=tuple(
            MediaReference(kind, file_id) for kind, file_id in json.loads(row["attachments"])
        ),
    )
    return InboxMessage(
        row["id"],
        key,
        perception,
        row["sender_name"],
        row["name"],
        datetime.fromisoformat(row["timestamp"]),
        "read" if row["is_read"] else "unread",
    )


class InboxStore:
    """使用数据根目录的 SQLite 保存消息，每次操作独立提交。"""

    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock
        with closing(open_connection(path)) as database:
            database.executescript(_SCHEMA)
            with database:
                database.execute("BEGIN IMMEDIATE")
                columns = database.execute("PRAGMA table_info(inbox_messages)")
                if all(column["name"] != "attachments" for column in columns):
                    database.execute(
                        "ALTER TABLE inbox_messages ADD COLUMN attachments "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )

    def add(
        self, perception: Perception, *, sender_name: str = "", conversation_name: str = ""
    ) -> InboxMessage | None:
        """保存入站消息，重复投递返回 None。"""
        key = ConversationKey.from_perception(perception)
        route = perception.route
        assert route is not None
        timestamp = self._clock.now()
        source_timestamp = perception.source_timestamp
        if source_timestamp is not None:
            try:
                timestamp = datetime.fromtimestamp(source_timestamp, UTC)
            except (OSError, OverflowError, ValueError):
                source_timestamp = None
        fallback = f"群 {key.recipient_id}" if key.kind == "group" else key.recipient_id
        with closing(open_connection(self._path)) as database, database:
            conversation_id, _ = database.execute(
                """INSERT INTO inbox_conversations
                (platform, account_id, kind, recipient_id, temporary_group_id, name)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (platform, account_id, kind, recipient_id, temporary_group_id)
                DO UPDATE SET name = CASE WHEN ? = '' THEN name ELSE excluded.name END
                RETURNING id, name""",
                (*key.parameters(), conversation_name or fallback, conversation_name),
            ).fetchone()
            added = database.execute(
                """INSERT INTO inbox_messages (conversation_id, message_id, sender_id, sender_name,
                content, visibility, trust, mentioned, quoted_message_id, source_timestamp,
                source_message_id, timestamp, attachments)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (conversation_id, message_id) DO NOTHING RETURNING id""",
                (
                    conversation_id,
                    route.message_id,
                    perception.sender_id,
                    sender_name or perception.sender_id,
                    perception.content,
                    perception.visibility,
                    perception.trust,
                    perception.mentioned,
                    perception.quoted_message_id,
                    source_timestamp,
                    perception.source_message_id,
                    timestamp.isoformat(),
                    json.dumps([(item.kind, item.file_id) for item in perception.attachments]),
                ),
            ).fetchone()
            if added is None:
                return None
            row = database.execute(f"{_MESSAGES} WHERE m.id = ?", (added["id"],)).fetchone()
            assert row is not None
            return _message(row)

    def rename(self, conversation: ConversationKey, name: str) -> None:
        """更新已经列入收件箱的会话名。"""
        with closing(open_connection(self._path)) as database, database:
            database.execute(
                f"UPDATE inbox_conversations SET name = ? WHERE {_KEY}",
                (name, *conversation.parameters()),
            )

    def list_conversations(self) -> list[InboxConversation]:
        """按最后收到消息的顺序列出会话，最近的在前。"""
        with closing(open_connection(self._path)) as database:
            rows = database.execute("""
                SELECT c.*, summary.unread_count, m.id AS last_message_id, m.timestamp
                FROM inbox_conversations c
                JOIN (SELECT conversation_id, MAX(id) AS last_id,
                             SUM(1 - is_read) AS unread_count
                      FROM inbox_messages GROUP BY conversation_id) summary
                ON summary.conversation_id = c.id
                JOIN inbox_messages m ON m.id = summary.last_id ORDER BY m.id DESC
            """).fetchall()
        return [
            InboxConversation(
                _conversation(row),
                row["name"],
                row["unread_count"],
                row["last_message_id"],
                datetime.fromisoformat(row["timestamp"]),
            )
            for row in rows
        ]

    def list_messages(
        self, conversation: ConversationKey, *, before_id: int | None = None, limit: int = 50
    ) -> list[InboxMessage]:
        """按收件箱序号倒序读取一页，游标边界不重复。"""
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"{_MESSAGES} WHERE {_KEY} AND (? IS NULL OR m.id < ?) ORDER BY m.id DESC LIMIT ?",
                (*conversation.parameters(), before_id, before_id, limit),
            ).fetchall()
        return [_message(row) for row in rows]

    def list_unread(self, conversation: ConversationKey, *, limit: int = 50) -> list[InboxMessage]:
        """从最早的未读开始读取一批。"""
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"{_MESSAGES} WHERE {_KEY} AND m.is_read = 0 ORDER BY m.id LIMIT ?",
                (*conversation.parameters(), limit),
            ).fetchall()
        return [_message(row) for row in rows]

    def list_priority_unread(
        self, platform: str, account_id: str, sender_ids: Iterable[str], *, limit: int = 50
    ) -> list[InboxMessage]:
        """读取指定平台账号下特别关心的未读消息。"""
        senders = tuple(sender_ids)
        if not senders:
            return []
        placeholders = ", ".join("?" for _ in senders)
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"{_MESSAGES} WHERE platform = ? AND account_id = ? AND m.is_read = 0 "
                f"AND sender_id IN ({placeholders}) ORDER BY m.id LIMIT ?",
                (platform, account_id, *senders, limit),
            ).fetchall()
        return [_message(row) for row in rows]

    def mark_read(self, ids: Iterable[int]) -> None:
        """确认心跳已经处理的消息，重复确认无副作用。"""
        with closing(open_connection(self._path)) as database, database:
            database.executemany(
                "UPDATE inbox_messages SET is_read = 1 WHERE id = ?",
                ((message_id,) for message_id in ids),
            )
