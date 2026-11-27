"""持久收件箱：按会话保留收到的感知和阅读状态。"""

import json
from collections.abc import Iterable
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection, Row
from typing import Literal

from aureus.common import Clock
from aureus.common.sqlite import open_connection

from .perception import (
    MediaReference,
    MessageComponent,
    MessageRoute,
    Perception,
    TrustLevel,
    VisibilityScope,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_conversations (
    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, account_id TEXT NOT NULL,
    kind TEXT NOT NULL, recipient_id TEXT NOT NULL, temporary_group_id TEXT NOT NULL,
    name TEXT NOT NULL, first_read_id INTEGER, last_read_id INTEGER,
    UNIQUE (platform, account_id, kind, recipient_id, temporary_group_id)
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES inbox_conversations(id),
    message_id TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
    content TEXT NOT NULL, visibility TEXT NOT NULL, trust TEXT NOT NULL,
    mentioned INTEGER NOT NULL, quoted_message_id TEXT, source_timestamp INTEGER,
    source_message_id TEXT, timestamp TEXT NOT NULL, received_at TEXT,
    attachments TEXT NOT NULL DEFAULT '[]',
    components TEXT NOT NULL DEFAULT '[]',
    direction TEXT NOT NULL DEFAULT 'incoming',
    UNIQUE (conversation_id, message_id)
);
CREATE INDEX IF NOT EXISTS inbox_messages_by_conversation
ON inbox_messages (conversation_id, id);
-- 会话列表按会话统计未读和最后一条；覆盖索引避免逐行回表，大收件箱从秒级降到百毫秒内（D129）。
CREATE INDEX IF NOT EXISTS inbox_messages_conversation_visibility
ON inbox_messages (conversation_id, id, direction, visibility);
-- 优先未读按会话查被点名的来信；部分索引只收需要的行，大收件箱也快（D137）。
CREATE INDEX IF NOT EXISTS inbox_messages_mentioned
ON inbox_messages (conversation_id, id) WHERE direction = 'incoming' AND mentioned = 1;
CREATE TABLE IF NOT EXISTS inbox_message_sequence (
    id INTEGER PRIMARY KEY CHECK (id = 1), high_water INTEGER NOT NULL
);
INSERT OR IGNORE INTO inbox_message_sequence SELECT 1, COALESCE(MAX(id), 0) FROM inbox_messages;
CREATE TRIGGER IF NOT EXISTS inbox_message_advance_sequence AFTER INSERT ON inbox_messages
BEGIN
    UPDATE inbox_message_sequence SET high_water = MAX(high_water, new.id) WHERE id = 1;
END;
CREATE TABLE IF NOT EXISTS inbox_history_exclusions (
    scope TEXT NOT NULL, source TEXT NOT NULL, source_key TEXT NOT NULL,
    PRIMARY KEY (scope, source, source_key)
);
CREATE TABLE IF NOT EXISTS inbox_history_cleanup_audit (
    proposal_id TEXT PRIMARY KEY, scope TEXT NOT NULL, visibility TEXT NOT NULL,
    cutoff TEXT NOT NULL, read_water INTEGER NOT NULL, inbox_water INTEGER NOT NULL,
    timeline_water INTEGER NOT NULL, originals INTEGER NOT NULL, timeline_only INTEGER NOT NULL,
    image_vectors INTEGER NOT NULL, confirmed_at TEXT NOT NULL
);
"""

_KEY = (
    "platform = ? AND account_id = ? AND kind = ? AND recipient_id = ? AND temporary_group_id = ?"
)
_UNREAD = "m.direction = 'incoming' AND m.id > COALESCE(c.last_read_id, 0)"
USER_VISIBLE_INBOX_MESSAGE = "m.visibility IN ('user_only', 'group', 'public')"
_SUMMARY_MESSAGE = f"{USER_VISIBLE_INBOX_MESSAGE} AND m.direction IN ('incoming', 'outgoing')"
_MESSAGES = f"""
SELECT m.*, c.platform, c.account_id, c.kind, c.recipient_id, c.temporary_group_id, c.name,
       NOT ({_UNREAD}) AS is_read
FROM inbox_messages m JOIN inbox_conversations c ON c.id = m.conversation_id
"""

_INVALIDATE_IMAGE_NOTES = """
CREATE TRIGGER IF NOT EXISTS inbox_image_notes_changed AFTER UPDATE OF attachments ON inbox_messages
WHEN OLD.attachments <> NEW.attachments
BEGIN
    UPDATE inbox_messages SET image_notes = (
        SELECT json_group_object(note.key, json(note.value)) FROM json_each(OLD.image_notes) note
        WHERE json_extract(OLD.attachments, '$[' || note.key || ']')
            IS json_extract(NEW.attachments, '$[' || note.key || ']')
    ) WHERE id = NEW.id;
END;
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

    def language_scope_key(self) -> str:
        """以完整路由区分平台、账号、群聊和私聊，不以名称或群号猜测。"""
        return json.dumps(self.parameters(), separators=(",", ":"))

    @classmethod
    def from_scope_key(cls, value: str) -> "ConversationKey":
        """读回本系统保存的完整会话键，不用显示名配对。"""
        platform, account_id, kind, recipient_id, temporary_group_id = json.loads(value)
        return cls(platform, account_id, kind, recipient_id, temporary_group_id or None)


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
    direction: str = "incoming"
    # 消息实际收到的时刻（D142）：防抖按它算批次起点，不用平台给的发送时间。
    received_at: datetime | None = None
    media_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class InboxImageNote:
    """原件某一位置已经取得的说明，或只能用于查询表情库的摘要。"""

    source: Literal["vlm", "emoji_catalog", "voice", "document", "video"]
    value: str


def _attachment_data(item: MediaReference) -> tuple[str | bool, ...]:
    base = (item.kind, item.file_id, item.is_emoji)
    return (*base, item.name) if item.name else base


def _note_source(item: MediaReference) -> str:
    return {
        "image": "emoji_catalog" if item.is_emoji else "vlm",
        "record": "voice",
        "file": "document",
        "video": "video",
    }[item.kind]


def _note_matches(item: MediaReference, source: str) -> bool:
    return source == _note_source(item) or (
        item.kind == "file" and source in ("voice", "vlm", "video")
    )


def _history_notes(perception: Perception, notes: dict[str, list[str]]) -> tuple[str, ...]:
    labels = {"image": "图片", "record": "语音", "file": "文件", "video": "视频"}
    result: list[str] = []
    for position, item in enumerate(perception.attachments):
        label = "表情" if item.is_emoji else labels[item.kind]
        source, text = notes.get(str(position), ["", ""])
        description = text if source != "emoji_catalog" and _note_matches(item, source) else ""
        result.append(f"[附件 {position + 1} · {label}] {description or '没有已取得的说明'}")
    return tuple(result)


def _conversation(row: Row) -> ConversationKey:
    return ConversationKey(
        row["platform"],
        row["account_id"],
        row["kind"],
        row["recipient_id"],
        row["temporary_group_id"] or None,
    )


def _find_visible_message(
    database: Connection, conversation: ConversationKey, identifier: int
) -> Row | None:
    return database.execute(
        f"{_MESSAGES} WHERE {_KEY} AND {USER_VISIBLE_INBOX_MESSAGE} AND m.id = ? "
        "AND m.direction != 'recalled'",
        (*conversation.parameters(), identifier),
    ).fetchone()


def _message(row: Row, *, history_notes: bool = False) -> InboxMessage:
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
        attachments=tuple(MediaReference(*value) for value in json.loads(row["attachments"])),
        components=tuple(MessageComponent(**value) for value in json.loads(row["components"])),
    )
    return InboxMessage(
        row["id"],
        key,
        perception,
        row["sender_name"],
        row["name"],
        datetime.fromisoformat(row["timestamp"]),
        "read" if row["is_read"] else "unread",
        row["direction"],
        datetime.fromisoformat(row["received_at"]) if row["received_at"] else None,
        _history_notes(perception, json.loads(row["image_notes"])) if history_notes else (),
    )


def _migrate_read_range(database: Connection) -> None:
    # 库里原来的零散已处理记录由时间线中的入箱序号去重。游标只跨过连续已读。
    database.execute("""
        WITH boundaries AS MATERIALIZED (
            SELECT conversation_id, MIN(id) FILTER (WHERE is_read = 0) AS first_unread_id
            FROM inbox_messages GROUP BY conversation_id
        ), ranges AS MATERIALIZED (
            SELECT m.conversation_id, MIN(m.id) AS first_id, MAX(m.id) AS last_id
            FROM inbox_messages m JOIN boundaries b ON b.conversation_id = m.conversation_id
            WHERE b.first_unread_id IS NULL OR m.id < b.first_unread_id GROUP BY m.conversation_id
        )
        UPDATE inbox_conversations AS c SET first_read_id = r.first_id, last_read_id = r.last_id
        FROM ranges r WHERE c.id = r.conversation_id
    """)
    database.execute("DROP INDEX IF EXISTS inbox_messages_unread")
    database.execute("ALTER TABLE inbox_messages DROP COLUMN is_read")


def _resolve_components(
    database: Connection, conversation_id: int, perception: Perception
) -> list[dict[str, object]]:
    resolved: list[dict[str, object]] = []
    for component in perception.components:
        if component.kind == "reply" and component.target_message_id is not None:
            original = database.execute(
                """SELECT sender_id FROM inbox_messages WHERE conversation_id = ?
                AND message_id = ? AND visibility = ? AND direction IN ('incoming','outgoing')""",
                (conversation_id, component.target_message_id, perception.visibility),
            ).fetchone()
            if original is not None:
                component = replace(component, target_account_id=original["sender_id"] or None)
        resolved.append(
            {key: value for key, value in asdict(component).items() if value is not None}
        )
    return resolved


def _advance_read_range(database: Connection, conversation_id: int, confirmed: set[int]) -> None:
    candidates = database.execute(
        f"""SELECT m.id FROM inbox_messages m JOIN inbox_conversations c ON c.id = m.conversation_id
        WHERE c.id = ? AND {_UNREAD} ORDER BY m.id LIMIT ?""",
        (conversation_id, len(confirmed)),
    )
    adjacent: list[int] = []
    for row in candidates:
        message_id = row["id"]
        if message_id not in confirmed:
            break
        adjacent.append(message_id)
    if adjacent:
        database.execute(
            """UPDATE inbox_conversations SET first_read_id = COALESCE(first_read_id, ?),
            last_read_id = ? WHERE id = ?""",
            (adjacent[0], adjacent[-1], conversation_id),
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
                columns = {
                    column["name"]
                    for column in database.execute("PRAGMA table_info(inbox_messages)")
                }
                if "attachments" not in columns:
                    database.execute(
                        "ALTER TABLE inbox_messages ADD COLUMN attachments "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )
                if "direction" not in columns:
                    database.execute(
                        "ALTER TABLE inbox_messages ADD COLUMN direction "
                        "TEXT NOT NULL DEFAULT 'incoming'"
                    )
                if "components" not in columns:
                    database.execute(
                        "ALTER TABLE inbox_messages ADD COLUMN components "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )
                if "received_at" not in columns:
                    database.execute("ALTER TABLE inbox_messages ADD COLUMN received_at TEXT")
                if "image_notes" not in columns:
                    database.execute(
                        "ALTER TABLE inbox_messages ADD COLUMN image_notes "
                        "TEXT NOT NULL DEFAULT '{}'"
                    )
                database.execute(_INVALIDATE_IMAGE_NOTES)
                if "is_read" in columns:
                    database.execute(
                        "ALTER TABLE inbox_conversations ADD COLUMN first_read_id INTEGER"
                    )
                    database.execute(
                        "ALTER TABLE inbox_conversations ADD COLUMN last_read_id INTEGER"
                    )
                    _migrate_read_range(database)

    def add(
        self, perception: Perception, *, sender_name: str = "", conversation_name: str = ""
    ) -> InboxMessage | None:
        """保存入站消息，重复投递返回 None。"""
        return self._add(perception, sender_name, conversation_name, "incoming")

    def add_delivered(self, perception: Perception, *, sender_name: str) -> None:
        """保存平台已确认的外发消息，不增加未读数。"""
        self._add(perception, sender_name, "", "outgoing")

    def find_platform_message(
        self, conversation: ConversationKey, platform_message_id: str
    ) -> InboxMessage | None:
        """按完整会话查入站或确已送达的原件，不推进已读水位。"""
        if not platform_message_id:
            return None
        with closing(open_connection(self._path, read_only=True)) as database:
            row = database.execute(
                f"""{_MESSAGES} WHERE {_KEY} AND m.message_id = ?
                    AND m.direction IN ('incoming', 'outgoing')""",
                (*conversation.parameters(), platform_message_id),
            ).fetchone()
        return _message(row) if row is not None else None

    def delivered_replies(
        self, conversation: ConversationKey, source_id: str
    ) -> list[InboxMessage]:
        """只返回本账号在本会话里直接回给指定原件的未撤回消息。"""
        if not source_id:
            return []
        with closing(open_connection(self._path, read_only=True)) as database:
            rows = database.execute(
                f"""{_MESSAGES} WHERE {_KEY} AND m.direction = 'outgoing'
                    AND m.sender_id = c.account_id AND m.source_message_id = ?
                    ORDER BY m.id""",
                (*conversation.parameters(), source_id),
            ).fetchall()
        return [_message(row) for row in rows]

    def history_revision(self, conversation: ConversationKey) -> int:
        """原件被撤回或清理时让该会话已有的工作记忆摘要失效。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            return database.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM inbox_history_exclusions WHERE scope=?",
                (conversation.language_scope_key(),),
            ).fetchone()[0]

    def nearby_before(self, message: InboxMessage, limit: int) -> list[InboxMessage]:
        """只读同会话同可见性的近期相邻入站原件；这些旁白不再排入回复队列。"""
        at = (message.received_at or message.timestamp).timestamp()
        with closing(open_connection(self._path, read_only=True)) as database:
            rows = database.execute(
                f"""{_MESSAGES} WHERE {_KEY} AND m.visibility = ? AND m.direction = 'incoming'
                    AND m.trust != ? AND m.id < ? AND unixepoch(COALESCE(m.received_at,m.timestamp))
                    BETWEEN ? AND ? ORDER BY m.id DESC LIMIT ?""",
                (
                    *message.conversation.parameters(),
                    message.perception.visibility,
                    TrustLevel.UNVERIFIED,
                    message.id,
                    at - 300,
                    at,
                    limit,
                ),
            ).fetchall()
        return [_message(row) for row in reversed(rows)]

    def has_newer_incoming(self, key: ConversationKey, after_id: int) -> bool:
        """已有新来信时，旧正文后的附图不再打断当前会话。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            return (
                database.execute(
                    "SELECT 1 FROM inbox_messages m JOIN inbox_conversations c "
                    f"ON m.conversation_id=c.id WHERE {_KEY} AND m.id>? "
                    "AND m.direction='incoming' LIMIT 1",
                    (*key.parameters(), after_id),
                ).fetchone()
                is not None
            )

    def mark_recalled(self, conversation: ConversationKey, platform_message_id: str) -> None:
        """记录平台消息撤回，支持通知先到且重复调用不改变收件箱水位。"""
        if not platform_message_id:
            return
        scope = conversation.language_scope_key()
        with closing(open_connection(self._path)) as database, database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT OR IGNORE INTO inbox_history_exclusions VALUES (?, 'recalled', ?)",
                (scope, platform_message_id),
            )
            database.execute(
                f"""UPDATE inbox_messages SET direction = 'recalled' WHERE conversation_id IN
                    (SELECT id FROM inbox_conversations WHERE {_KEY}) AND message_id = ?""",
                (*conversation.parameters(), platform_message_id),
            )

    def archive(
        self, perception: Perception, *, sender_name: str, conversation_name: str, notice: bool
    ) -> None:
        """只保存平台事件，不进入未读、特别关心和回复流程。"""
        self._add(perception, sender_name, conversation_name, "notice" if notice else "archived")

    def _add(
        self, perception: Perception, sender_name: str, conversation_name: str, direction: str
    ) -> InboxMessage | None:
        key = ConversationKey.from_perception(perception)
        route = perception.route
        assert route is not None
        received_at = self._clock.now()
        timestamp = received_at
        source_timestamp = perception.source_timestamp
        if source_timestamp is not None:
            try:
                timestamp = datetime.fromtimestamp(source_timestamp, UTC)
            except (OSError, OverflowError, ValueError):
                source_timestamp = None
        fallback = f"群 {key.recipient_id}" if key.kind == "group" else key.recipient_id
        with closing(open_connection(self._path)) as database, database:
            database.execute("BEGIN IMMEDIATE")
            if (
                database.execute(
                    "SELECT 1 FROM inbox_history_exclusions "
                    "WHERE scope=? AND source IN ('platform','recalled') AND source_key=?",
                    (key.language_scope_key(), route.message_id),
                ).fetchone()
                is not None
            ):
                return None
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
                """INSERT INTO inbox_messages (id, conversation_id, message_id, sender_id,
                sender_name,
                content, visibility, trust, mentioned, quoted_message_id, source_timestamp,
                source_message_id, timestamp, received_at, attachments, direction, components)
                VALUES ((SELECT high_water + 1 FROM inbox_message_sequence WHERE id=1),
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    received_at.isoformat(),
                    json.dumps([_attachment_data(item) for item in perception.attachments]),
                    direction,
                    json.dumps(_resolve_components(database, conversation_id, perception)),
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

    def list_conversations(self, *, visible_to_user: bool = False) -> list[InboxConversation]:
        """按最后收到消息的顺序列出会话，最近的在前。"""
        return self._list_conversations(only_unread=False, visible_to_user=visible_to_user)

    def list_unread_conversations(self) -> list[InboxConversation]:
        """列出仍有未读的会话，各会话按最早未读的入箱序号排列。"""
        return self._list_conversations(only_unread=True)

    def _list_conversations(
        self, *, only_unread: bool, visible_to_user: bool = False
    ) -> list[InboxConversation]:
        condition = "WHERE summary.first_unread_id IS NOT NULL" if only_unread else ""
        order = "summary.first_unread_id" if only_unread else "m.id DESC"
        visibility = f"WHERE {USER_VISIBLE_INBOX_MESSAGE}" if visible_to_user else ""
        with closing(open_connection(self._path)) as database:
            rows = database.execute(f"""
                SELECT c.*, summary.unread_count, m.id AS last_message_id, m.timestamp
                FROM inbox_conversations c
                JOIN (SELECT m.conversation_id, MAX(m.id) AS last_id,
                             COUNT(*) FILTER (WHERE {_UNREAD}) AS unread_count,
                             MIN(m.id) FILTER (WHERE {_UNREAD}) AS first_unread_id
                      FROM inbox_messages m JOIN inbox_conversations c ON c.id = m.conversation_id
                      {visibility}
                      GROUP BY m.conversation_id) summary
                ON summary.conversation_id = c.id
                JOIN inbox_messages m ON m.id = summary.last_id
                {condition} ORDER BY {order}
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

    def visible_conversation_keys(self) -> list[ConversationKey]:
        """只读取得网页可见会话键，调用方仍需核对账号和路由权限。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            rows = database.execute(f"""
                SELECT c.* FROM inbox_conversations c WHERE EXISTS (
                    SELECT 1 FROM inbox_messages m WHERE m.conversation_id = c.id
                    AND {USER_VISIBLE_INBOX_MESSAGE}
                )
            """)
            return [_conversation(row) for row in rows]

    def list_messages(
        self,
        conversation: ConversationKey,
        *,
        before_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
        visible_to_user: bool = False,
    ) -> list[InboxMessage]:
        """按收件箱序号倒序读取一页，游标边界不重复。"""
        visibility = f"AND {USER_VISIBLE_INBOX_MESSAGE}" if visible_to_user else ""
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"{_MESSAGES} WHERE {_KEY} {visibility} AND (? IS NULL OR m.id < ?) "
                "ORDER BY m.id DESC LIMIT ? OFFSET ?",
                (*conversation.parameters(), before_id, before_id, limit, offset),
            ).fetchall()
        return [_message(row) for row in rows]

    def count_visible_messages(self, conversation: ConversationKey) -> int:
        """统计完整路由中可供网页读取的规范原件。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            return int(
                database.execute(
                    f"SELECT COUNT(*) FROM inbox_messages m JOIN inbox_conversations c "
                    f"ON c.id = m.conversation_id WHERE {_KEY} AND {USER_VISIBLE_INBOX_MESSAGE}",
                    conversation.parameters(),
                ).fetchone()[0]
            )

    def last_incoming(
        self, key: ConversationKey, *, read_only: bool = False
    ) -> InboxMessage | None:
        """回看只认指定会话的真实来信；可要求它已被接收者读过。"""
        read = f" AND NOT ({_UNREAD})" if read_only else ""
        with closing(open_connection(self._path, read_only=True)) as database:
            row = database.execute(
                f"{_MESSAGES} WHERE {_KEY} AND m.direction='incoming'{read} "
                "ORDER BY m.id DESC LIMIT 1",
                key.parameters(),
            ).fetchone()
        return _message(row) if row else None

    def list_read_history(
        self,
        conversation: ConversationKey,
        visibility: VisibilityScope,
        *,
        before_id: int | None,
        keyword: str | None,
        limit: int,
    ) -> list[InboxMessage]:
        """读取同一可见范围内连续已读的来信与实际送达的回信，不改变水位。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            rows = database.execute(
                f"""{_MESSAGES} WHERE {_KEY} AND {_SUMMARY_MESSAGE} AND NOT ({_UNREAD})
                    AND m.visibility = ? AND (? IS NULL OR m.id < ?)
                    AND (? IS NULL OR instr(lower(m.content), lower(?)) > 0 OR EXISTS (
                        SELECT 1 FROM json_each(m.image_notes) n
                        WHERE n.value ->> '$[0]' IN ('vlm','voice','document','video')
                        AND instr(lower(n.value ->> '$[1]'), lower(?)) > 0))
                    ORDER BY m.id DESC LIMIT ?""",
                (
                    *conversation.parameters(),
                    visibility,
                    before_id,
                    before_id,
                    keyword,
                    keyword,
                    keyword,
                    limit,
                ),
            )
            return [_message(row, history_notes=True) for row in rows]

    def get_visible_message(
        self,
        conversation: ConversationKey,
        identifier: int,
    ) -> InboxMessage | None:
        """用会话完整路由和原件序号读取可见消息；不推进未读进度。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            row = _find_visible_message(database, conversation, identifier)
        return _message(row) if row is not None else None

    def get_learning_original(
        self, conversation: ConversationKey, identifier: int, connection: Connection | None = None
    ) -> InboxMessage | None:
        """供学习来源复核读取入站原件，包括不对网页公开的私聊。"""
        if connection is None:
            with closing(open_connection(self._path, read_only=True)) as database:
                return self.get_learning_original(conversation, identifier, database)
        row = connection.execute(
            f"{_MESSAGES} WHERE {_KEY} AND m.id = ? AND m.direction = 'incoming'",
            (*conversation.parameters(), identifier),
        ).fetchone()
        return _message(row) if row is not None else None

    def get_visible_image(
        self, conversation: ConversationKey, identifier: int, position: int
    ) -> tuple[Perception, MediaReference, InboxImageNote | None] | None:
        """从同一原件快照取附件及已有图注，不读图片字节。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            row = _find_visible_message(database, conversation, identifier)
        if row is None:
            return None
        perception = _message(row).perception
        if not 0 <= position < len(perception.attachments):
            return None
        attachment = perception.attachments[position]
        if attachment.kind != "image":
            return None
        note = json.loads(row["image_notes"]).get(str(position))
        return perception, attachment, InboxImageNote(*note) if note is not None else None

    def save_image_note(
        self, identifier: int, source: Perception, position: int, note: InboxImageNote
    ) -> None:
        """仅当会话、平台消息键及原位置附件仍匹配时保存已有识别。"""
        if not note.value or source.route is None or not source.route.message_id:
            return
        if not 0 <= position < len(source.attachments):
            return
        attachment = source.attachments[position]
        if not _note_matches(attachment, note.source):
            return
        with closing(open_connection(self._path)) as database, database:
            database.execute(
                f"""UPDATE inbox_messages AS m
                    SET image_notes = json_set(image_notes, ?, json(?))
                    WHERE id = ? AND message_id = ? AND direction = 'incoming'
                      AND {USER_VISIBLE_INBOX_MESSAGE}
                      AND conversation_id IN (SELECT id FROM inbox_conversations WHERE {_KEY})
                      AND json_extract(attachments, ?) = json(?)""",
                (
                    f'$."{position}"',
                    json.dumps((note.source, note.value), ensure_ascii=False),
                    identifier,
                    source.route.message_id,
                    *ConversationKey.from_perception(source).parameters(),
                    f"$[{position}]",
                    json.dumps(_attachment_data(attachment)),
                ),
            )

    def list_unread(self, conversation: ConversationKey, *, limit: int = 50) -> list[InboxMessage]:
        """从最早的未读开始读取一批。"""
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"{_MESSAGES} WHERE {_KEY} AND {_UNREAD} ORDER BY m.id LIMIT ?",
                (*conversation.parameters(), limit),
            ).fetchall()
        return [_message(row) for row in rows]

    def read_window(self, conversation: ConversationKey, *, limit: int) -> list[InboxMessage]:
        """从最早的未读开始读取一批，并在同一事务内推进到本批最后一条。"""
        with closing(open_connection(self._path)) as database, database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                f"{_MESSAGES} WHERE {_KEY} AND {_UNREAD} ORDER BY m.id LIMIT ?",
                (*conversation.parameters(), limit),
            ).fetchall()
            if not rows:
                return []
            messages = [_message(row) for row in rows]
            database.execute(
                f"""UPDATE inbox_conversations SET last_read_id = ?,
                first_read_id = COALESCE(first_read_id, (SELECT MIN(m.id) FROM inbox_messages m
                    WHERE m.conversation_id = inbox_conversations.id AND m.direction = 'incoming'))
                WHERE {_KEY}""",
                (messages[-1].id, *conversation.parameters()),
            )
            return messages

    def read_latest_and_clear(
        self, conversation: ConversationKey, *, limit: int
    ) -> list[InboxMessage]:
        """读取最新的 limit 条未读，并在同一事务内清空该会话的全部未读（D145）。

        大群消息来得比心跳快，从最早未读逐批追赶会让水位永远落在后面；这里只取最新一批，
        水位直接推到当前最大入站消息，更早没读到的直接标已读丢弃，下一轮从新未读开始。
        丢弃不等于删除：消息仍在收件箱里，已读历史仍可查询（D145 第四条）。

        Args:
            conversation: 完整会话路由。
            limit: 本次最多返回几条未读。

        Returns:
            最新的至多 limit 条未读，按入箱序号升序（时间顺序）；没有未读时返回空列表。
        """
        with closing(open_connection(self._path)) as database, database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                f"{_MESSAGES} WHERE {_KEY} AND {_UNREAD} ORDER BY m.id DESC LIMIT ?",
                (*conversation.parameters(), limit),
            ).fetchall()
            if not rows:
                return []
            messages = [_message(row) for row in reversed(rows)]
            database.execute(
                f"""UPDATE inbox_conversations SET last_read_id = (
                    SELECT MAX(m.id) FROM inbox_messages m
                    WHERE m.conversation_id = inbox_conversations.id
                      AND m.direction = 'incoming'
                ), first_read_id = COALESCE(first_read_id, (SELECT MIN(m.id) FROM inbox_messages m
                    WHERE m.conversation_id = inbox_conversations.id AND m.direction = 'incoming'))
                WHERE {_KEY}""",
                conversation.parameters(),
            )
            return messages

    def summary_scopes(self) -> list[tuple[ConversationKey, VisibilityScope]]:
        """列出已保存且有可处理可见性的会话范围，不返回任何消息正文。"""
        with closing(open_connection(self._path)) as database:
            rows = database.execute(f"""
                SELECT c.*, m.visibility FROM inbox_messages m
                JOIN inbox_conversations c ON c.id = m.conversation_id
                WHERE {_SUMMARY_MESSAGE} GROUP BY c.id, m.visibility ORDER BY min(m.id)
            """).fetchall()
        return [(_conversation(row), VisibilityScope(row["visibility"])) for row in rows]

    def summary_window(
        self,
        conversation: ConversationKey,
        visibility: VisibilityScope,
        after_id: int,
        limit: int,
        *,
        connection: Connection | None = None,
    ) -> tuple[InboxMessage, ...]:
        """读取同一会话和可见范围的正序窗口，可在记忆提交事务中重验。"""
        if connection is None:
            with closing(open_connection(self._path)) as database:
                return self.summary_window(
                    conversation, visibility, after_id, limit, connection=database
                )
        rows = connection.execute(
            f"""{_MESSAGES} WHERE {_KEY} AND {_SUMMARY_MESSAGE}
                AND m.visibility=? AND m.id>?
                ORDER BY m.id LIMIT ?""",
            (*conversation.parameters(), visibility, after_id, limit),
        )
        return tuple(_message(row) for row in rows)

    def summary_evidence(
        self,
        connection: Connection,
        conversation: ConversationKey,
        visibility: VisibilityScope,
        sequence: int,
        message_id: str,
    ) -> InboxMessage | None:
        """在同一个读写事务定位证据；已清理原件不回退到缓存正文。"""
        row = connection.execute(
            f"""{_MESSAGES} WHERE {_KEY} AND {_SUMMARY_MESSAGE} AND m.visibility=?
            AND m.id=? AND m.message_id=?""",
            (*conversation.parameters(), visibility, sequence, message_id),
        ).fetchone()
        return _message(row) if row is not None else None

    def existing_message_ids(self, conversation: ConversationKey, ids: Iterable[str]) -> set[str]:
        """核对旧消息是否已有同一完整路由的规范原件，不读取正文。"""
        encoded = json.dumps(tuple(ids))
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"""SELECT m.message_id FROM inbox_messages m
                JOIN inbox_conversations c ON c.id = m.conversation_id WHERE {_KEY}
                AND m.message_id IN (SELECT value FROM json_each(?))
                UNION SELECT source_key FROM inbox_history_exclusions
                WHERE scope = ? AND source IN ('platform','recalled')
                AND source_key IN (SELECT value FROM json_each(?))""",
                (*conversation.parameters(), encoded, conversation.language_scope_key(), encoded),
            ).fetchall()
        return {row[0] for row in rows}

    def excluded_timeline_ids(self, conversation: ConversationKey, ids: Iterable[int]) -> set[int]:
        """读取已从该会话平台视图移除的旧时间线定位。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            return {
                int(row[0])
                for row in database.execute(
                    """SELECT source_key FROM inbox_history_exclusions
                       WHERE scope = ? AND source = 'timeline'
                       AND source_key IN (SELECT CAST(value AS TEXT) FROM json_each(?))""",
                    (conversation.language_scope_key(), json.dumps(tuple(ids))),
                )
            }

    @property
    def database_path(self) -> Path:
        """平台保留策略需要与原件、旧时间线及关联缓存使用同一事务。"""
        return self._path

    def list_priority_unread(
        self,
        conversation: ConversationKey,
        sender_ids: Iterable[str],
        *,
        after_id: int | None = None,
        limit: int = 50,
    ) -> list[InboxMessage]:
        """按入箱顺序分页读取指定会话内已验证发件人的优先未读。"""
        senders = tuple(sender_ids)
        if not senders:
            return []
        placeholders = ", ".join("?" for _ in senders)
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"{_MESSAGES} WHERE {_KEY} AND {_UNREAD} AND m.id > ? "
                f"AND m.trust != ? AND m.sender_id IN ({placeholders}) ORDER BY m.id LIMIT ?",
                (*conversation.parameters(), after_id or 0, TrustLevel.UNVERIFIED, *senders, limit),
            ).fetchall()
        return [_message(row) for row in rows]

    def list_mentioned_unread(
        self,
        conversation: ConversationKey,
        *,
        after_id: int | None = None,
        limit: int = 50,
    ) -> list[InboxMessage]:
        """按入箱顺序分页读取指定会话内已验证发件人的被点名未读（D137）。"""
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"{_MESSAGES} WHERE {_KEY} AND {_UNREAD} AND m.id > ? "
                "AND m.trust != ? AND m.mentioned = 1 ORDER BY m.id LIMIT ?",
                (*conversation.parameters(), after_id or 0, TrustLevel.UNVERIFIED, limit),
            ).fetchall()
        return [_message(row) for row in rows]

    def list_mention_conversations(
        self,
        limit: int = 5,
        *,
        pending_ids: Iterable[int] = (),
        special_accounts: Iterable[tuple[str, str]] = (),
    ) -> list[tuple[ConversationKey, str, int, int]]:
        """分开统计提及和仅特别关心；同条来信不因已入队或同时满足两者而多计。"""
        with closing(open_connection(self._path, read_only=True)) as database:
            rows = database.execute(
                """SELECT c.*, SUM(m.mentioned) AS mention_count,
                          COUNT(*) - SUM(m.mentioned) AS special_count
                   FROM inbox_messages m JOIN inbox_conversations c ON c.id = m.conversation_id
                   WHERE m.direction = 'incoming' AND m.trust != ?
                     AND (m.mentioned = 1 OR EXISTS (
                         SELECT 1 FROM json_each(?) AS account
                         WHERE json_extract(account.value, '$[0]') = c.platform
                           AND json_extract(account.value, '$[1]') = m.sender_id))
                     AND (m.id > COALESCE(c.last_read_id, 0)
                          OR m.id IN (SELECT value FROM json_each(?)))
                   GROUP BY c.id ORDER BY COUNT(*) DESC, MIN(m.id) LIMIT ?""",
                (
                    TrustLevel.UNVERIFIED,
                    json.dumps(tuple(special_accounts)),
                    json.dumps(tuple(pending_ids)),
                    limit,
                ),
            )
            return [
                (_conversation(row), row["name"], row["mention_count"], row["special_count"])
                for row in rows
            ]

    def list_attention_unread(
        self,
        conversation: ConversationKey,
        bound_senders: Iterable[str],
        *,
        special_senders: Iterable[str] = (),
        after: tuple[int, int] | None = None,
        limit: int = 50,
    ) -> list[InboxMessage]:
        """绑定用户优先，特别关心、提及与引用的来信按入箱顺序（D149）。

        Args:
            conversation: 完整会话路由。
            bound_senders: 该平台上绑定用户的账号。
            special_senders: 该平台上特别关心的账号。
            after: 上一页最后一行的（优先档，入箱序号）；0 是绑定用户，1 是其余优先来信。
            limit: 本页最多几行。

        Returns:
            每条消息只返回一次；两档内部都按入箱序号升序。
        """
        owners = tuple(bound_senders)
        senders = {*owners, *special_senders}
        after_rank, after_id = after if after is not None else (None, None)
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"""WITH candidates AS (
                    SELECT m.*, c.platform, c.account_id, c.kind, c.recipient_id,
                           c.temporary_group_id, c.name, NOT ({_UNREAD}) AS is_read,
                           CASE WHEN m.sender_id IN (SELECT value FROM json_each(?))
                                THEN 0 ELSE 1 END AS identity_rank
                    FROM inbox_messages m JOIN inbox_conversations c ON c.id = m.conversation_id
                    WHERE {_KEY} AND {_UNREAD} AND m.trust != ?
                      AND (m.sender_id IN (SELECT value FROM json_each(?)) OR m.mentioned = 1
                           OR (m.quoted_message_id IS NOT NULL AND EXISTS (
                               SELECT 1 FROM inbox_messages own
                               WHERE own.conversation_id = m.conversation_id
                               AND own.message_id = m.quoted_message_id
                               AND own.direction = 'outgoing'
                               AND own.sender_id = c.account_id AND own.visibility = m.visibility)))
                )
                SELECT * FROM candidates
                WHERE ? IS NULL OR identity_rank > ? OR (identity_rank = ? AND id > ?)
                ORDER BY identity_rank, id LIMIT ?""",
                (
                    json.dumps(owners),
                    *conversation.parameters(),
                    TrustLevel.UNVERIFIED,
                    json.dumps(tuple(senders)),
                    after_rank,
                    after_rank,
                    after_rank,
                    after_id,
                    limit,
                ),
            ).fetchall()
        return [_message(row) for row in rows]

    def mark_read(self, ids: Iterable[int]) -> None:
        """把已确认且相邻的消息计入连续已读范围，保留中间的未读。"""
        confirmed = set(ids)
        if not confirmed:
            return
        with closing(open_connection(self._path)) as database, database:
            database.execute("BEGIN IMMEDIATE")
            conversations = database.execute(
                """SELECT DISTINCT conversation_id FROM inbox_messages
                WHERE id IN (SELECT value FROM json_each(?))""",
                (json.dumps(sorted(confirmed)),),
            ).fetchall()
            for conversation in conversations:
                _advance_read_range(database, conversation["conversation_id"], confirmed)
