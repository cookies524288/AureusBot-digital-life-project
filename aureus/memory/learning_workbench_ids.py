"""学习工作台整数编号：只记录权威条目的命名空间和稳定键，不复制资料。"""

from collections.abc import Iterable
from sqlite3 import Connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_workbench_ids (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    UNIQUE (kind, entry_key)
);
CREATE TABLE IF NOT EXISTS learning_workbench_metadata (
    kind TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    origin_scope TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (kind, entry_key)
);
"""


def identify(connection: Connection, kind: str, keys: Iterable[str]) -> dict[str, int]:
    """在同一数据库为已核对条目分配稳定整数，不复用旧编号。"""
    entries = set(keys)
    if not entries:
        return {}
    known = {
        row["entry_key"]: row["id"]
        for row in connection.execute(
            "SELECT entry_key, id FROM learning_workbench_ids WHERE kind = ?", (kind,)
        )
    }
    if entries - known.keys():
        for key in sorted(entries - known.keys()):
            row = connection.execute(
                "INSERT INTO learning_workbench_ids (kind, entry_key) VALUES (?, ?) "
                "ON CONFLICT(kind, entry_key) DO NOTHING RETURNING id",
                (kind, key),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT id FROM learning_workbench_ids WHERE kind = ? AND entry_key = ?",
                    (kind, key),
                ).fetchone()
            known[key] = row["id"]
    return {key: known[key] for key in entries}


def resolve(connection: Connection, kind: str, identifiers: Iterable[int]) -> dict[int, str]:
    """反查既有编号；调用方仍须复验条目存在及当前访问边界。"""
    return {
        identifier: row["entry_key"]
        for identifier in set(identifiers)
        if (
            row := connection.execute(
                "SELECT entry_key FROM learning_workbench_ids WHERE kind = ? AND id = ?",
                (kind, identifier),
            ).fetchone()
        )
        is not None
    }


def initialize(connection: Connection) -> None:
    """创建工作台编号表；它只保存命名空间与稳定键。"""
    connection.executescript(SCHEMA)
