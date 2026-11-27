"""QQ 会话范围内的聊天工具与定时回看，不建立第二条发送通道（D147）。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Row
from typing import Literal

from aureus.common import Clock, HeartbeatConfig
from aureus.common.sqlite import open_connection

from .chat_capabilities import (
    GroupMembership,
    MemeEntry,
    find_group_memberships,
    format_meme_result,
    lookup_meme,
)
from .inbox_store import ConversationKey, InboxMessage, InboxStore
from .perception import Perception, TrustLevel, VisibilityScope

type GroupReader = Callable[[Perception], Awaitable[list[tuple[str, str]]]]
type AssetNames = Callable[[ConversationKey], tuple[str, ...]]

_MEMES = (
    MemeEntry(
        "赛博朋克",
        "常用来调侃高科技与破败生活并存的强烈反差。",
        "Aureus 自编通用释义，不代表首次出处",
    ),
    MemeEntry(
        "摸鱼",
        "指在该忙的时候暂时偷闲，也可用来轻松自嘲。",
        "Aureus 自编通用释义，不代表首次出处",
    ),
    MemeEntry(
        "破防",
        "形容因感动、难受或被戳中而绷不住了，需结合语境。",
        "Aureus 自编通用释义，不代表首次出处",
    ),
    MemeEntry(
        "真香",
        "先说不想要，后来却很喜欢时用来自嘲或打趣。",
        "Aureus 自编通用释义，不代表首次出处",
    ),
    MemeEntry(
        "吃瓜",
        "通常指旁观一场热闹或争议，不一定参与其中。",
        "Aureus 自编通用释义，不代表首次出处",
    ),
    MemeEntry(
        "立flag",
        "公开放话或作出承诺；也常用来调侃话音刚落就被现实反转。",
        "Aureus 自编通用释义，不代表首次出处",
    ),
    MemeEntry(
        "电子榨菜",
        "吃饭时顺便看的轻松视频、节目等，不是指食物。",
        "Aureus 自编通用释义，不代表首次出处",
    ),
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, source TEXT NOT NULL,
    message TEXT NOT NULL, actor TEXT NOT NULL, target TEXT NOT NULL, note TEXT NOT NULL,
    due REAL NOT NULL, UNIQUE(kind, source, message)
);
CREATE INDEX IF NOT EXISTS chat_pending_due ON chat_pending(due) WHERE kind <> 'nudge';
CREATE INDEX IF NOT EXISTS chat_pending_target ON chat_pending(target, kind);
"""
_MAX_PENDING = 20
_UNAVAILABLE = "未获准、目标不存在或参数不适用，没有执行。"


@dataclass(frozen=True)
class ChatRequest:
    """规划工具的请求；目标和时限还要按来信原件复核。"""

    operation: str
    text: str = ""
    seconds: int = 0
    identifier: int = 0
    target_kind: Literal["group", "private"] | None = None
    target_id: str = ""


@dataclass(frozen=True)
class PendingChat:
    """已登记的提醒、叮嘱或一次性回看，来源和目的地均为完整会话键。"""

    id: int
    kind: str
    source: ConversationKey
    message: str
    actor: str
    target: ConversationKey
    note: str
    due: float

    @classmethod
    def from_row(cls, row: Row) -> "PendingChat":
        """将收件箱数据库中的登记还原成会话范围。"""
        return cls(
            row["id"],
            row["kind"],
            ConversationKey.from_scope_key(row["source"]),
            row["message"],
            row["actor"],
            ConversationKey.from_scope_key(row["target"]),
            row["note"],
            row["due"],
        )


class ChatPluginTools:
    """将请求锁定到实际入站原件；定时项到期只产生一次重看机会。"""

    def __init__(self, inbox: InboxStore, clock: Clock, config: HeartbeatConfig) -> None:
        self.inbox, self.clock, self.config = inbox, clock, config
        self.allows: Callable[[ConversationKey], bool] = lambda _: False
        self.is_owner: Callable[[str], bool] = lambda _: False
        self.groups: GroupReader | None = None
        self.assets: AssetNames | None = None
        with closing(open_connection(inbox.database_path)) as database:
            database.executescript(_SCHEMA)
            self._next_due = self._earliest(
                database.execute(
                    "SELECT min(due) FROM chat_pending WHERE kind <> 'nudge'"
                ).fetchone()
            )

    @staticmethod
    def _earliest(row: Row) -> float:
        return float("inf") if row[0] is None else row[0]

    def remaining(self) -> float:
        """下一次提醒或回看离心跳时钟还有几秒。"""
        return max(0.0, self._next_due - self.clock.now().timestamp())

    def permitted(self, source: Perception) -> bool:
        """核对渠道、权限、发送者与仍可读的入站原件。"""
        route = source.route
        if (
            route is None
            or source.channel != "qq"
            or route.kind not in ("group", "private")
            or route.temporary_group_id
            or source.trust is TrustLevel.UNVERIFIED
        ):
            return False
        key = ConversationKey.from_perception(source)
        if not self.allows(key):
            return False
        original = self.inbox.find_platform_message(key, route.message_id)
        return (
            original is not None
            and original.direction == "incoming"
            and original.perception.sender_id == source.sender_id
            and original.perception.trust is source.trust
            and original.perception.visibility is source.visibility
            and original.perception.has_shared_content == source.has_shared_content
            and (source.trust is not TrustLevel.VERIFIED_USER or self.owner(source))
        )

    def owner(self, source: Perception) -> bool:
        """只接受当前仍绑定主人的本人普通私聊。"""
        route = source.route
        return (
            route is not None
            and route.kind == "private"
            and not route.temporary_group_id
            and route.recipient_id == source.sender_id
            and source.trust is TrustLevel.VERIFIED_USER
            and source.visibility is VisibilityScope.USER_ONLY
            and self.is_owner(source.sender_id)
        )

    def operations(self, source: Perception, *, writable: bool) -> tuple[str, ...]:
        """只声明这个发言者在这一轮可以使用的操作。"""
        if not self.permitted(source):
            return ()
        writable = writable and not source.has_shared_content
        names: list[str] = []
        if self.config.chat_meme_lookup_enabled:
            names.append("meme")
        if (
            self.config.chat_group_lookup_enabled
            and source.route
            and (source.route.kind == "group" or (self.groups is not None and self.owner(source)))
        ):
            names.append("groups")
        if (
            self.config.chat_assets_enabled
            and self.assets is not None
            and not source.has_shared_content
        ):
            names.append("assets")
        if self.config.chat_reminders_enabled:
            names.append("reminders")
            if writable:
                names.extend(("create_reminder", "cancel_reminder"))
        if writable and self.config.chat_nudges_enabled and self.owner(source):
            names.append("owner_nudge")
        return tuple(names)

    async def run(self, source: Perception, req: ChatRequest, *, writable: bool) -> str:
        """执行二次授权后的查询或登记，返回能实际证实的结果。"""
        if req.operation not in self.operations(source, writable=writable):
            return _UNAVAILABLE
        key = ConversationKey.from_perception(source)
        if req.operation == "meme":
            local = tuple(
                MemeEntry(name.strip(), meaning.strip(), "维护者配置的可公开本地词典")
                for name, meaning in self.config.chat_meme_glossary.items()
                if name.strip() and meaning.strip()
            )
            return format_meme_result(lookup_meme((*local, *_MEMES), req.text))
        if req.operation == "groups" and not self.owner(source):
            original = (
                self.inbox.find_platform_message(key, source.route.message_id)
                if source.route
                else None
            )
            if (
                original
                and key.kind == "group"
                and req.text
                and req.text in (key.recipient_id, original.conversation_name)
            ):
                return "是，我在当前这个群。"
            return "不能确认；不能在这里查看其他群的成员资格或群名单。"
        if req.operation == "groups" and self.groups is not None:
            groups = await self.groups(source)
            if not self.permitted(source) or not self.owner(source):
                return _UNAVAILABLE
            results = find_group_memberships(
                [GroupMembership(number, name) for number, name in groups if number and name],
                req.text,
                limit=20,
            )
            return json.dumps(
                {
                    "groups": [(item.group_id, item.name[:80]) for item in results.matches],
                    "total": results.total_matches,
                },
                ensure_ascii=False,
            )
        if req.operation == "assets" and self.assets is not None:
            return json.dumps(await asyncio.to_thread(self.assets, key), ensure_ascii=False)
        target = key
        if req.target_kind or req.target_id:
            if (
                not req.target_kind
                or not req.target_id
                or not self.owner(source)
                or req.operation not in ("create_reminder", "owner_nudge")
            ):
                return _UNAVAILABLE
            target = ConversationKey(key.platform, key.account_id, req.target_kind, req.target_id)
            if not self.allows(target) or self.inbox.last_incoming(target, read_only=True) is None:
                return _UNAVAILABLE
        if req.operation == "reminders":
            with closing(open_connection(self.inbox.database_path, read_only=True)) as db:
                rows = db.execute(
                    "SELECT id, note, due FROM chat_pending WHERE kind = 'reminder' "
                    "AND source = ? AND actor = ? ORDER BY due LIMIT ?",
                    (key.language_scope_key(), source.sender_id, _MAX_PENDING),
                )
                return json.dumps(
                    [
                        {
                            "id": row["id"],
                            "note": row["note"],
                            "due": datetime.fromtimestamp(
                                row["due"], self.clock.now().tzinfo
                            ).isoformat(),
                        }
                        for row in rows
                    ],
                    ensure_ascii=False,
                )
        if req.operation == "cancel_reminder":
            with closing(open_connection(self.inbox.database_path)) as db, db:
                deleted = db.execute(
                    "DELETE FROM chat_pending WHERE kind = 'reminder' AND id = ? "
                    "AND source = ? AND actor = ?",
                    (req.identifier, key.language_scope_key(), source.sender_id),
                ).rowcount
            return "已取消。" if deleted else "没有可取消的此项提醒。"
        nudge = req.operation == "owner_nudge"
        if nudge and target == key:
            return _UNAVAILABLE
        if (
            req.operation not in ("owner_nudge", "create_reminder")
            or not req.text
            or not 1 <= req.seconds <= (3600 if nudge else 31_536_000)
        ):
            return _UNAVAILABLE
        identifier = self._add(
            "nudge" if nudge else "reminder", source, target, req.text, req.seconds
        )
        if identifier is None:
            return "没有新建；该来信已处理或已达待办上限。"
        return f"已登记 #{identifier}；仅供目标会话判断，不保证发言。"

    def _add(
        self, kind: str, source: Perception, target: ConversationKey, note: str, seconds: int
    ) -> int | None:
        if source.route is None or not self.permitted(source) or not self.allows(target):
            return None
        source_key = ConversationKey.from_perception(source).language_scope_key()
        now = self.clock.now().timestamp()
        due = now + seconds
        with closing(open_connection(self.inbox.database_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM chat_pending WHERE kind='nudge' AND due <= ?", (now,))
            count = db.execute(
                "SELECT count(*) FROM chat_pending WHERE source=? AND actor=?",
                (source_key, source.sender_id),
            ).fetchone()[0]
            if count >= _MAX_PENDING:
                return None
            cursor = db.execute(
                "INSERT OR IGNORE INTO chat_pending(kind,source,message,actor,target,note,due) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    kind,
                    source_key,
                    source.route.message_id,
                    source.sender_id,
                    target.language_scope_key(),
                    note,
                    due,
                ),
            )
            if not cursor.rowcount:
                return None
            if kind != "nudge":
                self._next_due = min(self._next_due, due)
            return cursor.lastrowid

    def recheck(self, kind: Literal["wait", "followup"], source: Perception, seconds: int) -> None:
        """只给仍是最新来信的原件登记一次回看。"""
        if not self.config.chat_rechecks_enabled or not 1 <= seconds <= 300 or source.route is None:
            return
        key = ConversationKey.from_perception(source)
        latest = self.inbox.last_incoming(key)
        if latest and latest.perception.route == source.route:
            self._add(kind, source, key, "", seconds)

    def new_message(self, message: InboxMessage) -> None:
        """新来信取消本会话之前的等待或续话，不影响显式提醒。"""
        if self._next_due == float("inf") or not message.perception.route:
            return
        with closing(open_connection(self.inbox.database_path)) as db, db:
            db.execute(
                "DELETE FROM chat_pending WHERE kind IN ('wait','followup') "
                "AND target=? AND message <> ?",
                (message.conversation.language_scope_key(), message.perception.route.message_id),
            )

    def _still_valid(self, item: PendingChat) -> bool:
        if not self.allows(item.target) or not self.allows(item.source):
            return False
        source = self.inbox.find_platform_message(item.source, item.message)
        if not source or source.direction != "incoming" or not self.permitted(source.perception):
            return False
        if source.perception.sender_id != item.actor:
            return False
        return item.target == item.source or self.owner(source.perception)

    def nudge(self, target: ConversationKey) -> PendingChat | None:
        """返回仍在时限且来源与目标都获准的下一条主人叮嘱。"""
        if not self.config.chat_nudges_enabled or not self.allows(target):
            return None
        with closing(open_connection(self.inbox.database_path, read_only=True)) as db:
            rows = db.execute(
                "SELECT * FROM chat_pending WHERE kind = 'nudge' AND target = ? "
                "AND due > ? ORDER BY id LIMIT ?",
                (target.language_scope_key(), self.clock.now().timestamp(), _MAX_PENDING),
            ).fetchall()
        return next(
            (item for row in rows if self._still_valid(item := PendingChat.from_row(row))), None
        )

    def nudge_authorized(self, item: PendingChat) -> bool:
        """回复送出前复核叮嘱的权限和时限。"""
        return (
            self.config.chat_nudges_enabled
            and item.due > self.clock.now().timestamp()
            and self._still_valid(item)
        )

    def current(self, item: PendingChat, anchor: Perception) -> bool:
        """到期后仍在原来的会话；非显式提醒还必须没有新来信。"""
        if (
            not anchor.route
            or ConversationKey.from_perception(anchor) != item.target
            or not self._still_valid(item)
        ):
            return False
        if item.kind == "reminder":
            return self.config.chat_reminders_enabled
        latest = self.inbox.last_incoming(item.target)
        return bool(
            self.config.chat_rechecks_enabled and latest and latest.perception.route == anchor.route
        )

    def consume_nudge(self, target: ConversationKey, identifier: int) -> None:
        """成功回执只消费本次发言实际使用的目的会话叮嘱。"""
        with closing(open_connection(self.inbox.database_path)) as db, db:
            db.execute(
                "DELETE FROM chat_pending WHERE kind='nudge' AND target=? AND id=?",
                (target.language_scope_key(), identifier),
            )

    def take_due(self) -> tuple[PendingChat, InboxMessage] | None:
        """原子取出一条到期登记与可读原件；交给心跳判断，不代发。"""
        if self.remaining() > 0:
            return None
        with closing(open_connection(self.inbox.database_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM chat_pending WHERE kind <> 'nudge' AND due <= ? "
                "ORDER BY due,id LIMIT 1",
                (self.clock.now().timestamp(),),
            ).fetchone()
            if row:
                db.execute("DELETE FROM chat_pending WHERE id=?", (row["id"],))
            self._next_due = self._earliest(
                db.execute("SELECT min(due) FROM chat_pending WHERE kind <> 'nudge'").fetchone()
            )
        if not row:
            return None
        item = PendingChat.from_row(row)
        if not self._still_valid(item):
            return None
        if item.kind == "reminder" and not self.config.chat_reminders_enabled:
            return None
        anchor = self.inbox.last_incoming(item.target, read_only=True)
        if item.kind != "reminder":
            latest = self.inbox.last_incoming(item.target)
            if (
                not self.config.chat_rechecks_enabled
                or not latest
                or not latest.perception.route
                or latest.perception.route.message_id != item.message
            ):
                return None
            anchor = latest if latest.status == "read" else None
        return (item, anchor) if anchor and self.permitted(anchor.perception) else None
