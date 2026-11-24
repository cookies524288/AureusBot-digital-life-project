"""网页表达簇的逐条展示授权，与召回、语言包或图谱授权无关。"""

from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from aureus.common.sqlite import open_connection
from aureus.memory import LanguageStore
from aureus.memory.expression_index import display_version
from aureus.memory.language_matching import LanguageEntry


class ExpressionClusterDisplay:
    """仅保存已获准的原件指纹；不保存表达正文或登录凭据。"""

    def __init__(self, path: Path, language: LanguageStore) -> None:
        self._path = path
        self._language = language
        with closing(open_connection(path)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS web_expression_cluster_grants (
                    entry_key TEXT PRIMARY KEY NOT NULL,
                    fingerprint TEXT NOT NULL
                )
            """)

    @staticmethod
    def _visible(entry: LanguageEntry, scopes: frozenset[str]) -> bool:
        return entry.visible_in(None) or any(entry.visible_in(scope) for scope in scopes)

    def access(self, scopes: frozenset[str]) -> dict[str, str]:
        """仅返回当前仍符合授权内容和来源范围的原件。"""
        with closing(open_connection(self._path, read_only=True)) as connection:
            connection.execute("BEGIN")
            saved = dict(
                connection.execute(
                    "SELECT entry_key, fingerprint FROM web_expression_cluster_grants"
                )
            )
            if not saved:
                return {}
            return {
                entry.entry_key: saved[entry.entry_key]
                for entry in self._language.expression_pool(connection)
                if entry.entry_key in saved
                and display_version(entry) == saved[entry.entry_key]
                and self._visible(entry, scopes)
            }

    def set_grant(
        self, key: str, mark: str | None, scopes: frozenset[str], active: Callable[[], bool]
    ) -> bool:
        """授权之前复核原件；撤权不依赖原件仍可用。"""
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if mark is not None and not any(
                entry.entry_key == key
                and display_version(entry) == mark
                and self._visible(entry, scopes)
                for entry in self._language.expression_pool(connection)
            ):
                return False
            if not active():
                return False
            if mark is None:
                connection.execute(
                    "DELETE FROM web_expression_cluster_grants WHERE entry_key = ?", (key,)
                )
            else:
                connection.execute(
                    "INSERT INTO web_expression_cluster_grants VALUES (?, ?) "
                    "ON CONFLICT(entry_key) DO UPDATE SET fingerprint = excluded.fingerprint",
                    (key, mark),
                )
        return True

    def selection(self, key: str, scopes: frozenset[str]) -> tuple[str | None, bool]:
        """返回具体选择的当前版本及是否有一笔可撤回授权，不返回正文。"""
        with closing(open_connection(self._path, read_only=True)) as connection:
            connection.execute("BEGIN")
            saved = (
                connection.execute(
                    "SELECT 1 FROM web_expression_cluster_grants WHERE entry_key = ?", (key,)
                ).fetchone()
                is not None
            )
            mark = next(
                (
                    display_version(entry)
                    for entry in self._language.expression_pool(connection)
                    if entry.entry_key == key and self._visible(entry, scopes)
                ),
                None,
            )
            return mark, saved
