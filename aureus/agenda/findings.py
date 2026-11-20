"""探索收获存储：正文只记一句，分享标记只能写一次（D79）。"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureus.agenda.store import parse, stamp
from aureus.common import Clock
from aureus.common.limits import MAX_FINDING_CHARS
from aureus.common.sqlite import open_connection


@dataclass(frozen=True)
class Finding:
    """一条收获及其来源和分享记录。"""

    id: int
    interest_id: int
    text: str
    page_title: str
    page_url: str
    created_at: datetime
    shared_at: datetime | None
    shared_record_id: int | None


class FindingStore:
    """收获表只属于日程子系统。"""

    def __init__(self, db_path: Path, clock: Clock) -> None:
        self._db_path, self._clock = db_path, clock
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """打开连接并创建表和触发器。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, interest_id INTEGER NOT NULL,
                    text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND {MAX_FINDING_CHARS}),
                    page_title TEXT NOT NULL CHECK(length(page_title) <= {MAX_FINDING_CHARS}),
                    page_url TEXT NOT NULL, created_at TEXT NOT NULL,
                    shared_at TEXT, shared_record_id INTEGER,
                    CHECK((shared_at IS NULL) = (shared_record_id IS NULL))
                );
                CREATE TRIGGER IF NOT EXISTS no_delete_findings BEFORE DELETE ON findings
                BEGIN SELECT RAISE(ABORT, 'Findings cannot be deleted'); END;
                CREATE TRIGGER IF NOT EXISTS no_rewrite_findings BEFORE UPDATE ON findings
                WHEN OLD.shared_at IS NOT NULL OR NEW.shared_at IS NULL
                  OR OLD.id IS NOT NEW.id OR OLD.interest_id IS NOT NEW.interest_id
                  OR OLD.text IS NOT NEW.text OR OLD.page_title IS NOT NEW.page_title
                  OR OLD.page_url IS NOT NEW.page_url OR OLD.created_at IS NOT NEW.created_at
                BEGIN SELECT RAISE(ABORT, 'Findings are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_replace_findings BEFORE INSERT ON findings
                WHEN EXISTS(SELECT 1 FROM findings WHERE id = NEW.id)
                BEGIN SELECT RAISE(ABORT, 'Findings cannot be replaced'); END;
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
            raise RuntimeError("收获表还没有打开")
        return self._conn

    def add(self, interest_id: int, text: str, page_title: str, page_url: str) -> Finding:
        """保存一句收获，标题截到 200 字。"""
        with self._require_open() as conn:
            cursor = conn.execute(
                "INSERT INTO findings(interest_id, text, page_title, page_url, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    interest_id,
                    text.strip(),
                    page_title[:MAX_FINDING_CHARS],
                    page_url,
                    stamp(self._clock.now()),
                ),
            )
            row = conn.execute(
                "SELECT * FROM findings WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _finding(row)

    def recent(self, limit: int) -> list[Finding]:
        """最新收获在前。"""
        return [
            _finding(row)
            for row in self._require_open().execute(
                "SELECT * FROM findings ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    def oldest_unshared(self, since: datetime) -> Finding | None:
        """时间窗内最早的一条未分享收获。"""
        row = (
            self._require_open()
            .execute(
                "SELECT * FROM findings WHERE created_at >= ? AND shared_at IS NULL"
                " ORDER BY id LIMIT 1",
                (stamp(since),),
            )
            .fetchone()
        )
        return _finding(row) if row is not None else None

    def mark_shared(self, finding_id: int, record_id: int, at: datetime) -> bool:
        """只标记一次；已分享过或不存在返回 False。"""
        with self._require_open() as conn:
            cursor = conn.execute(
                "UPDATE findings SET shared_at = ?, shared_record_id = ?"
                " WHERE id = ? AND shared_at IS NULL",
                (stamp(at), record_id, finding_id),
            )
        return bool(cursor.rowcount)


def _finding(row: sqlite3.Row) -> Finding:
    values = dict(row)
    values["created_at"] = datetime.fromisoformat(row["created_at"])
    values["shared_at"] = parse(row["shared_at"])
    return Finding(**values)
