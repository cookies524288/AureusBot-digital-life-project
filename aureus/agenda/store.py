"""日程：它答应过的事（D74，路线图 2.7）。

表 agenda_items 只属于日程子系统，放在 aureus.db 里。内容、对象、到期时间、来源写入后不许改；
状态只能从 open 改一次；不许删，数据库触发器保证（同反馈表）。这里只管存取，不碰时间线。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from pathlib import Path

from aureus.common import AgendaConfig, Clock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import open_connection

_TIMESTAMP_PRECISION = "microseconds"
# 条目内容最多这么长：只是一句话的约定，太长说明模型在抄对话（D74 第 3 条）。
MAX_CONTENT_CHARS = 200
# 规划器写到期时间用的格式（当地时间）。
DUE_FORMAT = "%Y-%m-%d %H:%M"


class ItemStatus(StrEnum):
    """条目的状态。open 以外都是结局，只能从 open 改一次。"""

    OPEN = "open"
    DONE = "done"  # 做完了：承诺成功
    DROPPED = "dropped"  # 做不了，说明了原因：承诺失败
    EXPIRED = "expired"  # 到期很久没交代：承诺失败
    CANCELLED = "cancelled"  # 用户取消：不算成败


class AgendaError(Exception):
    """日程条目违反规则（内容为空、改已经结束的条目等）。"""


@dataclass(frozen=True)
class AgendaItem:
    """日程表里的一行。"""

    id: int
    person_id: int
    content: str
    created_at: datetime
    due_at: datetime | None
    status: ItemStatus
    closed_at: datetime | None
    reason: str
    source_record_id: int | None
    feedback_id: int | None
    due_noticed: bool

    def is_due(self, now: datetime) -> bool:
        """到期了没有（还没结束、到期时间已过）。"""
        return self.status is ItemStatus.OPEN and self.due_at is not None and self.due_at <= now


def parse_due(
    text: object, now: datetime, timezone: tzinfo | None, max_days: int
) -> datetime | None:
    """把规划器写的到期时间（当地「YYYY-MM-DD HH:MM」）换成 UTC 时刻。

    看不懂、早于现在、超过 max_days 天，都返回 None：只丢掉时间，条目照记（D74 第 3 条）。
    """
    if not isinstance(text, str):
        return None
    try:
        local = datetime.strptime(text.strip(), DUE_FORMAT)
    except ValueError:
        return None
    zone = timezone if timezone is not None else now.astimezone().tzinfo
    due = local.replace(tzinfo=zone)
    if due < now or due > now + timedelta(days=max_days):
        return None
    return due


class AgendaStore:
    """日程存储：记条目、查未完成的、标到期提醒、结束条目。"""

    def __init__(self, db_path: Path, clock: Clock, config: AgendaConfig) -> None:
        self._db_path = db_path
        self._clock = clock
        self._config = config
        self._conn: sqlite3.Connection | None = None

    @property
    def config(self) -> AgendaConfig:
        """日程配置。"""
        return self._config

    def open(self) -> None:
        """打开数据库连接并建表。已经打开时什么也不做。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            self._init_schema(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        statuses = ", ".join(f"'{status.value}'" for status in ItemStatus)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS agenda_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                content TEXT NOT NULL CHECK (length(content) > 0),
                created_at TEXT NOT NULL,
                due_at TEXT,
                status TEXT NOT NULL CHECK (status IN ({statuses})),
                closed_at TEXT,
                reason TEXT NOT NULL DEFAULT '',
                source_record_id INTEGER,
                feedback_id INTEGER,
                due_noticed INTEGER NOT NULL DEFAULT 0 CHECK (due_noticed IN (0, 1))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agenda_person ON agenda_items(person_id, status)"
        )
        # 约定本身写下后不许改；状态只能从 open 改一次；提醒标记只能从 0 变 1（D74 第 4 条）。
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS no_rewrite_agenda_items
            BEFORE UPDATE ON agenda_items
            WHEN OLD.id IS NOT NEW.id
              OR OLD.person_id IS NOT NEW.person_id
              OR OLD.content IS NOT NEW.content
              OR OLD.created_at IS NOT NEW.created_at
              OR OLD.due_at IS NOT NEW.due_at
              OR OLD.source_record_id IS NOT NEW.source_record_id
              OR OLD.feedback_id IS NOT NEW.feedback_id
              OR (OLD.status != 'open' AND OLD.status IS NOT NEW.status)
              OR (OLD.status != 'open' AND OLD.closed_at IS NOT NEW.closed_at)
              OR (OLD.status != 'open' AND OLD.reason IS NOT NEW.reason)
              OR (OLD.due_noticed = 1 AND NEW.due_noticed = 0)
            BEGIN
                SELECT RAISE(ABORT, 'Agenda items are immutable; status can change once');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS no_delete_agenda_items
            BEFORE DELETE ON agenda_items
            BEGIN
                SELECT RAISE(ABORT, 'Agenda items cannot be deleted');
            END
            """
        )
        conn.commit()

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("日程还没有打开")
        return self._conn

    def add(
        self,
        person_id: int,
        content: str,
        *,
        due_at: datetime | None,
        source_record_id: int | None,
        feedback_id: int | None,
    ) -> AgendaItem | None:
        """记一条新的约定；这个人未完成的条目已经满了时不记，返回 None。

        Raises:
            AgendaError: 内容是空的。
        """
        text = " ".join(content.split())[:MAX_CONTENT_CHARS]
        if not text:
            raise AgendaError("日程条目的内容不能是空的")
        conn = self._require_open()
        with conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM agenda_items WHERE person_id = ? AND status = 'open'",
                (person_id,),
            ).fetchone()[0]
            if count >= self._config.max_open_per_person:
                return None
            cursor = conn.execute(
                "INSERT INTO agenda_items (person_id, content, created_at, due_at, status,"
                " source_record_id, feedback_id) VALUES (?, ?, ?, ?, 'open', ?, ?)",
                (
                    person_id,
                    text,
                    stamp(self._clock.now()),
                    stamp(due_at) if due_at is not None else None,
                    source_record_id,
                    feedback_id,
                ),
            )
        assert cursor.lastrowid is not None
        item = self.get(cursor.lastrowid)
        assert item is not None
        return item

    def get(self, item_id: int) -> AgendaItem | None:
        """按编号取一条；没有就是 None。"""
        row = (
            self._require_open()
            .execute("SELECT * FROM agenda_items WHERE id = ?", (item_id,))
            .fetchone()
        )
        return None if row is None else _item_of(row)

    def open_items(self, person_id: int | None = None) -> list[AgendaItem]:
        """未完成的条目，先到期的在前（没有到期时间的排最后）；不给 person_id 就是所有人的。"""
        sql = "SELECT * FROM agenda_items WHERE status = 'open'"
        params: tuple[object, ...] = ()
        if person_id is not None:
            sql += " AND person_id = ?"
            params = (person_id,)
        sql += " ORDER BY due_at IS NULL, due_at, id"
        return [_item_of(row) for row in self._require_open().execute(sql, params).fetchall()]

    def recent(self, limit: int = 50) -> list[AgendaItem]:
        """最新的若干条（不管状态），新的在前；给命令行看。"""
        rows = (
            self._require_open()
            .execute("SELECT * FROM agenda_items ORDER BY id DESC LIMIT ?", (limit,))
            .fetchall()
        )
        return [_item_of(row) for row in rows]

    def mark_due_noticed(self, item_id: int) -> None:
        """记下这条的到期已经提醒过（每条只提醒一次）。"""
        conn = self._require_open()
        with conn:
            conn.execute("UPDATE agenda_items SET due_noticed = 1 WHERE id = ?", (item_id,))

    def finish(self, item_id: int, status: ItemStatus, reason: str = "") -> AgendaItem | None:
        """结束一条未完成的条目；已经结束或不存在时返回 None，不报错。

        Raises:
            AgendaError: 状态不是结局（open），或者放弃时没写原因（D74 第 6 条）。
        """
        if status is ItemStatus.OPEN:
            raise AgendaError("结束条目时状态不能是 open")
        text = " ".join(reason.split())[:MAX_CONTENT_CHARS]
        if status is ItemStatus.DROPPED and not text:
            raise AgendaError("放弃承诺必须说明原因")
        conn = self._require_open()
        with conn:
            cursor = conn.execute(
                "UPDATE agenda_items SET status = ?, closed_at = ?, reason = ?"
                " WHERE id = ? AND status = 'open'",
                (status.value, stamp(self._clock.now()), text, item_id),
            )
        if cursor.rowcount == 0:
            return None
        return self.get(item_id)

    def overdue(self, now: datetime) -> list[AgendaItem]:
        """到期后过了宽限还没交代的条目（该算过期了）。"""
        horizon = now - timedelta(days=self._config.expire_grace_days)
        rows = (
            self._require_open()
            .execute(
                "SELECT * FROM agenda_items WHERE status = 'open' AND due_at IS NOT NULL"
                " AND due_at <= ? ORDER BY due_at, id",
                (stamp(horizon),),
            )
            .fetchall()
        )
        return [_item_of(row) for row in rows]

    def due_unnoticed(self, now: datetime) -> list[AgendaItem]:
        """已到期、还没提醒过的未完成条目。"""
        rows = (
            self._require_open()
            .execute(
                "SELECT * FROM agenda_items WHERE status = 'open' AND due_noticed = 0"
                " AND due_at IS NOT NULL AND due_at <= ? ORDER BY due_at, id",
                (stamp(now),),
            )
            .fetchall()
        )
        return [_item_of(row) for row in rows]


def stamp(moment: datetime) -> str:
    """把带时区的时间转成统一精度的 UTC 字符串，供日程各表存储。"""
    return ensure_aware(moment).astimezone(UTC).isoformat(timespec=_TIMESTAMP_PRECISION)


def parse(text: str | None) -> datetime | None:
    """还原日程表中的 ISO 时间字符串；空值保持为 None。"""
    return None if text is None else datetime.fromisoformat(text)


def _item_of(row: sqlite3.Row) -> AgendaItem:
    return AgendaItem(
        id=int(row["id"]),
        person_id=int(row["person_id"]),
        content=str(row["content"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        due_at=parse(row["due_at"]),
        status=ItemStatus(row["status"]),
        closed_at=parse(row["closed_at"]),
        reason=str(row["reason"]),
        source_record_id=row["source_record_id"],
        feedback_id=row["feedback_id"],
        due_noticed=bool(row["due_noticed"]),
    )
