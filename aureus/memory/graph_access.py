"""从发布事务记录的原件快照管理独立的关系图展示授权。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from aureus.common import Clock
from aureus.common.sqlite import open_connection
from aureus.memory.history import MemorySource

type GraphCategory = Literal["entities", "relations", "evidence"]
GRAPH_CATEGORIES: tuple[GraphCategory, ...] = ("entities", "relations", "evidence")
_SOURCE_SIGNATURE = """json_array(
    history.content, history.status, history.valid_from, history.valid_to,
    graph.kind, graph.status, graph.name, graph.subject_source, graph.object_source,
    graph.predicate, graph.confidence, graph.evidence
)"""
_JOIN_GRAPH = "LEFT JOIN memory_graph_sources graph ON graph.source_key = history.source_key"


@dataclass(frozen=True)
class GraphAccess:
    """一次查询可用的展示范围；版本防止撤回后恢复相同范围时复用旧响应。"""

    generation: int
    sources: tuple[MemorySource, ...]


@dataclass(frozen=True)
class GraphBatch:
    """供维护者选择的已发布批次标识，不包含图资料和未授权计数。"""

    batch_id: str
    instance: str
    categories: tuple[GraphCategory, ...]


def open_graph_access(connection: sqlite3.Connection) -> None:
    """建立独立授权表，不扫描或回填旧发布数据。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memory_graph_batches (
                instance TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, batch_id TEXT UNIQUE NOT NULL,
                entities INTEGER NOT NULL DEFAULT 0, relations INTEGER NOT NULL DEFAULT 0,
                evidence INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memory_graph_batch_members (
                batch_id TEXT NOT NULL, source_key TEXT NOT NULL, category TEXT NOT NULL,
                signature TEXT NOT NULL, PRIMARY KEY (batch_id, source_key)
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memory_graph_access_state (
                id INTEGER PRIMARY KEY CHECK (id = 1), generation INTEGER NOT NULL
            )
        """)
        connection.execute("INSERT OR IGNORE INTO memory_graph_access_state VALUES (1, 0)")


def _signature(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _note_change(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE memory_graph_access_state SET generation = generation + 1")


def save_graph_batch(
    connection: sqlite3.Connection,
    snapshot: tuple[str, str],
    members: dict[str, GraphCategory],
) -> None:
    """在发布事务内记下本批图来源与内容指纹；新版本不继承授权。"""
    instance, fingerprint = snapshot
    if members:
        signatures = connection.execute(
            f"""SELECT history.source_key, {_SOURCE_SIGNATURE} FROM memory_history history
                {_JOIN_GRAPH} WHERE history.source_key IN (SELECT value FROM json_each(?))
                ORDER BY history.source_key""",
            (json.dumps(list(members)),),
        )
        rows = [(key, members[key], _signature(content)) for key, content in signatures]
    else:
        rows = []
    previous = connection.execute(
        "SELECT batch_id, fingerprint FROM memory_graph_batches WHERE instance = ?", (instance,)
    ).fetchone()
    if previous is not None:
        old_id, old_fingerprint = previous
        old = connection.execute(
            """SELECT source_key, category, signature FROM memory_graph_batch_members
               WHERE batch_id = ? ORDER BY source_key""",
            (old_id,),
        )
        if fingerprint == old_fingerprint and rows == [tuple(row) for row in old]:
            return
        connection.execute("DELETE FROM memory_graph_batch_members WHERE batch_id = ?", (old_id,))
        connection.execute("DELETE FROM memory_graph_batches WHERE batch_id = ?", (old_id,))
        _note_change(connection)
    if rows:
        batch_id = uuid4().hex
        connection.execute(
            "INSERT INTO memory_graph_batches(instance, fingerprint, batch_id) VALUES (?, ?, ?)",
            (instance, fingerprint, batch_id),
        )
        connection.executemany(
            "INSERT INTO memory_graph_batch_members VALUES (?, ?, ?, ?)",
            ((batch_id, *row) for row in rows),
        )


def read_graph_access(connection: sqlite3.Connection, now: datetime) -> GraphAccess:
    """按当前事务快照核对授权、原件内容、时间和停用状态。"""
    state = connection.execute("SELECT generation FROM memory_graph_access_state").fetchone()
    generation = state[0]
    if not connection.execute(
        "SELECT 1 FROM memory_graph_batches WHERE entities OR relations OR evidence LIMIT 1"
    ).fetchone():
        return GraphAccess(generation, ())
    rows = connection.execute(
        f"""SELECT member.source_key, member.signature, {_SOURCE_SIGNATURE}
            FROM memory_graph_batches batch
            JOIN memory_history_snapshots snapshot USING (instance, fingerprint)
            JOIN memory_graph_batch_members member USING (batch_id)
            JOIN memory_history history ON history.source_key = member.source_key
            {_JOIN_GRAPH}
            LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = history.id
            WHERE CASE member.category WHEN 'entities' THEN batch.entities
                        WHEN 'relations' THEN batch.relations
                        WHEN 'evidence' THEN batch.evidence END
              AND (history.status = 'active' OR (history.status = 'missing' AND (
                  graph.kind = 'entity' OR json_extract(history.source_key, '$[2]') IN
                      ('paragraph_entities', 'paragraph_relations'))))
              AND (history.valid_from IS NULL OR history.valid_from <= ?)
              AND (history.valid_to IS NULL OR history.valid_to > ?)
              AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)
              AND NOT coalesce(edit.is_bundle_removed, 0) AND edit.edited_content IS NULL
            ORDER BY member.source_key""",
        (now.timestamp(), now.timestamp()),
    )
    return GraphAccess(generation, tuple(
        MemorySource.decode(key) for key, signature, content in rows
        if signature == _signature(content)
    ))


class MemoryGraphDisplay:
    """持久化的网页展示授权；不参与记忆检索、召回或知识包安装。"""

    def __init__(self, database: Path, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    def list_batches(self) -> list[GraphBatch]:
        """列出服务端真实登记的批次，不从图或搜索结果推测批次。"""
        with closing(open_connection(self._database, read_only=True)) as connection:
            return [
                GraphBatch(row["batch_id"], row["instance"], tuple(
                    category for category in GRAPH_CATEGORIES if row[category]
                ))
                for row in connection.execute("""
                    SELECT batch.* FROM memory_graph_batches batch
                    JOIN memory_history_snapshots snapshot USING (instance, fingerprint)
                    ORDER BY instance
                """)
            ]

    def set_categories(self, batch_id: str, categories: tuple[GraphCategory, ...]) -> bool:
        """替换指定发布快照的展示类别；空类别撤权，不修改图资料。"""
        with closing(open_connection(self._database)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT batch.* FROM memory_graph_batches batch
                JOIN memory_history_snapshots snapshot USING (instance, fingerprint)
                WHERE batch_id = ?
            """, (batch_id,)).fetchone()
            if row is None:
                return False
            selected = tuple(category in categories for category in GRAPH_CATEGORIES)
            if selected != tuple(bool(row[category]) for category in GRAPH_CATEGORIES):
                connection.execute("""
                    UPDATE memory_graph_batches SET entities = ?, relations = ?, evidence = ?
                    WHERE batch_id = ?
                """, (*selected, batch_id))
                _note_change(connection)
            return True

    def access(self) -> GraphAccess:
        """读取当下有效的展示授权及其版本。"""
        with closing(open_connection(self._database, read_only=True)) as connection:
            connection.execute("BEGIN")
            return read_graph_access(connection, self._clock.now())
