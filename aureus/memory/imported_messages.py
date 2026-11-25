"""旧迁入原件的消息专用读取，不走全文、记忆覆盖或召回。"""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
from sqlite3 import Connection

from aureus.common.sqlite import open_connection

from .history import HistoricalMemory, decode_history_row
from .message_boundary import is_message_source

_VISIBLE_MESSAGE = f"""{is_message_source("history.source_key")}
    AND history.status = 'active' AND coalesce(edit.is_enabled, 1)
    AND NOT coalesce(edit.is_removed, 0)
    AND NOT coalesce(edit.is_bundle_removed, 0)"""


class ImportedPlatformHistory:
    """历史投影未保留可核验的收发账号和回址，只提供独立未归类页。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _has_message_tables(self, database: Connection) -> bool:
        tables = {
            row["name"]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('memory_history','memory_edits')"
            )
        }
        return "memory_history" in tables and "memory_edits" in tables

    def has_any(self) -> bool:
        """是否有仍获准使用的迁入消息；只查第一行，不读正文（D121）。"""
        with closing(open_connection(self._path)) as database:
            if not self._has_message_tables(database):
                return False
            row = database.execute(
                f"""SELECT 1 FROM memory_history AS history
                   LEFT JOIN memory_edits AS edit
                     ON edit.kind = 'history' AND edit.entry_id = history.id
                   WHERE {_VISIBLE_MESSAGE} LIMIT 1"""
            ).fetchone()
        return row is not None

    def page(self, *, before_id: int | None, limit: int, offset: int = 0) -> list[HistoricalMemory]:
        """按导入序号分页读仍获准使用的原文，不用记忆编辑内容代替原文。"""
        with closing(open_connection(self._path)) as database:
            if not self._has_message_tables(database):
                return []
            rows = database.execute(
                f"""SELECT history.* FROM memory_history AS history
                   LEFT JOIN memory_edits AS edit
                     ON edit.kind = 'history' AND edit.entry_id = history.id
                   WHERE {_VISIBLE_MESSAGE}
                     AND (? IS NULL OR history.id < ?)
                   ORDER BY history.id DESC LIMIT ? OFFSET ?""",
                (before_id, before_id, limit, offset),
            ).fetchall()
        return [replace(decode_history_row(row), entry_id=row["id"]) for row in rows]
