"""兴趣存储：起始兴趣同步与只追加的自行改动（D79）。"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureus.agenda.store import _parse, _stamp
from aureus.common import Clock, InterestsConfig
from aureus.common.sqlite import open_connection

logger = logging.getLogger(__name__)
MAX_INTEREST_CHARS = 30


@dataclass(frozen=True)
class Interest:
    """兴趣表的一行；结束后不能复活或改字。"""

    id: int
    text: str
    origin: str
    status: str
    created_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    replaced_by: int | None
    source_record_id: int | None


class InterestStore:
    """日程子系统管理的兴趣表；不写时间线。"""

    def __init__(self, db_path: Path, clock: Clock, config: InterestsConfig) -> None:
        self._db_path, self._clock, self.config = db_path, clock, config
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """打开连接并创建表和不可改写触发器。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS interests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 30),
                    origin TEXT NOT NULL CHECK(origin IN ('seed', 'self')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'removed')),
                    created_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT,
                    replaced_by INTEGER, source_record_id INTEGER,
                    CHECK(end_reason IS NULL OR end_reason IN
                        ('user_removed', 'self_removed', 'replaced'))
                );
                CREATE TRIGGER IF NOT EXISTS no_delete_interests BEFORE DELETE ON interests
                BEGIN SELECT RAISE(ABORT, 'Interests cannot be deleted'); END;
                CREATE TRIGGER IF NOT EXISTS no_rewrite_interests BEFORE UPDATE ON interests
                WHEN OLD.status != 'active' OR NEW.status != 'removed'
                  OR OLD.id IS NOT NEW.id OR OLD.text IS NOT NEW.text
                  OR OLD.origin IS NOT NEW.origin OR OLD.created_at IS NOT NEW.created_at
                  OR OLD.source_record_id IS NOT NEW.source_record_id
                BEGIN SELECT RAISE(ABORT, 'Interests are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_replace_interests BEFORE INSERT ON interests
                WHEN EXISTS(SELECT 1 FROM interests WHERE id = NEW.id)
                BEGIN SELECT RAISE(ABORT, 'Interests cannot be replaced'); END;
            """)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        """关闭连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("兴趣表还没有打开")
        return self._conn

    def active(self) -> list[Interest]:
        """返回在用兴趣，按编号升序。"""
        return [
            _interest(row)
            for row in self._require_open().execute(
                "SELECT * FROM interests WHERE status = 'active' ORDER BY id"
            )
        ]

    def get(self, interest_id: int) -> Interest | None:
        """按编号读取，缺失返回 None。"""
        row = (
            self._require_open()
            .execute("SELECT * FROM interests WHERE id = ?", (interest_id,))
            .fetchone()
        )
        return _interest(row) if row is not None else None

    def _can_add(self, text: str) -> bool:
        return 1 <= len(text) <= MAX_INTEREST_CHARS and all(
            item.text != text for item in self.active()
        )

    def _insert(self, text: str, origin: str, source: int | None) -> Interest:
        cursor = self._require_open().execute(
            "INSERT INTO interests(text, origin, created_at, source_record_id) VALUES (?, ?, ?, ?)",
            (text, origin, _stamp(self._clock.now()), source),
        )
        assert cursor.lastrowid is not None
        item = self.get(cursor.lastrowid)
        assert item is not None
        return item

    def add(self, text: str, origin: str, source_record_id: int | None = None) -> Interest | None:
        """新增合法且不重复的兴趣；容量已满时返回 None。"""
        text = text.strip()
        with self._require_open():
            if not self._can_add(text) or len(self.active()) >= self.config.max_active:
                return None
            return self._insert(text, origin, source_record_id)

    def remove(
        self, interest_id: int, reason: str, replaced_by: int | None = None
    ) -> Interest | None:
        """结束在用兴趣；已经结束或不存在时返回 None。"""
        with self._require_open() as conn:
            changed = conn.execute(
                "UPDATE interests SET status = 'removed', ended_at = ?, end_reason = ?,"
                " replaced_by = ? WHERE id = ? AND status = 'active'",
                (_stamp(self._clock.now()), reason, replaced_by, interest_id),
            )
        return self.get(interest_id) if changed.rowcount else None

    def replace(self, interest_id: int, new_text: str, source_record_id: int) -> Interest | None:
        """同一事务里新增、结束旧兴趣；替换不增加在用总数（D79）。"""
        text = new_text.strip()
        with self._require_open() as conn:
            old = self.get(interest_id)
            if old is None or old.status != "active" or not self._can_add(text):
                return None
            if len(self.active()) > self.config.max_active:
                return None
            new = self._insert(text, "self", source_record_id)
            conn.execute(
                "UPDATE interests SET status = 'removed', ended_at = ?, end_reason = 'replaced',"
                " replaced_by = ? WHERE id = ?",
                (_stamp(self._clock.now()), new.id, interest_id),
            )
            return new

    def sync_seeds(self, texts: list[str]) -> tuple[int, int]:
        """同步文件中的起始兴趣；任何来源曾出现过的话都不重新加入。"""
        removed = sum(
            self.remove(item.id, "user_removed") is not None
            for item in self.active()
            if item.origin == "seed" and item.text not in texts
        )
        added = 0
        for text in texts:
            if (
                self._require_open()
                .execute("SELECT 1 FROM interests WHERE text = ? LIMIT 1", (text,))
                .fetchone()
            ):
                continue
            if self.add(text, "seed") is None:
                logger.warning("起始兴趣超过容量或不合格，跳过一条")
            else:
                added += 1
        return added, removed


def _interest(row: sqlite3.Row) -> Interest:
    values = dict(row)
    values["created_at"] = datetime.fromisoformat(row["created_at"])
    values["ended_at"] = _parse(row["ended_at"])
    return Interest(**values)
