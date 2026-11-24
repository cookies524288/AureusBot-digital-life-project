"""事务安装与可逆卸载记忆包，归属状态独立于人工停用和移除。"""

import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from typing import Any, BinaryIO

from aureus.memory.bundle_format import (
    BundleMode,
    MemoryBundle,
    MemoryBundleConflictError,
    bundle_digest,
    load_bundle,
    write_bundle,
)
from aureus.memory.bundle_projection import BundleProjection, project_bundle
from aureus.memory.graph import put_graph_sources
from aureus.memory.history import MemorySource, put_history_record


def open_bundles(connection: sqlite3.Connection) -> None:
    """建立安装账与归属；原件仍只存入现有记忆表。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memory_bundles (
                installation_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, version TEXT NOT NULL,
                name TEXT NOT NULL, content_level TEXT NOT NULL, digest TEXT NOT NULL,
                installed_at TEXT NOT NULL, changed_at TEXT NOT NULL, installed INTEGER NOT NULL,
                manifest TEXT NOT NULL, knowledge TEXT NOT NULL, state TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memory_bundle_members (
                installation_id TEXT NOT NULL, entry_id INTEGER NOT NULL,
                PRIMARY KEY (installation_id, entry_id)
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS memory_bundle_members_entry
            ON memory_bundle_members (entry_id, installation_id)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memory_bundle_owned (entry_id INTEGER PRIMARY KEY)
        """)


def _installation(connection: sqlite3.Connection, installation_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM memory_bundles WHERE installation_id = ?", (installation_id,)
    ).fetchone()


def _restore_reason(connection: sqlite3.Connection, bundle: MemoryBundle) -> str:
    if bundle.content_level != "full":
        return "只有完整包能使用空库恢复。"
    if connection.execute("""
        SELECT EXISTS(SELECT 1 FROM memory_history) OR EXISTS(SELECT 1 FROM memory_index)
    """).fetchone()[0]:
        return "空库恢复要求目标记忆库没有原件（包括已移除、已卸载的原件）。"
    return ""


def _inspect(
    connection: sqlite3.Connection, bundle: MemoryBundle, projection: BundleProjection
) -> dict[str, Any]:
    installed = _installation(connection, bundle.installation_id)
    if installed is not None and installed["digest"] != bundle.digest:
        raise MemoryBundleConflictError("同 ID、同版本已经登记了不同内容，请使用新的包版本。")
    return {
        "installation_id": bundle.installation_id,
        "package_id": bundle.package_id,
        "name": bundle.name,
        "version": bundle.version,
        "format_version": 1,
        "content_level": bundle.content_level,
        "counts": dict(Counter(source.table for source in projection.records)),
        "status": "new"
        if installed is None
        else ("installed" if installed["installed"] else "uninstalled"),
        "restore_blocked": _restore_reason(connection, bundle),
    }


def preview_bundle(connection: sqlite3.Connection, file: BinaryIO) -> dict[str, Any]:
    """核验读包、可投影内容及安装模式，不写数据库。"""
    bundle = load_bundle(file)
    projection = project_bundle(bundle)
    with connection:
        connection.execute("BEGIN")
        return _inspect(connection, bundle, projection)


def _entry_id_with_ownership(
    connection: sqlite3.Connection, source: MemorySource
) -> tuple[int, bool] | None:
    row = connection.execute(
        """
        SELECT original.id, owned.entry_id IS NOT NULL
        FROM memory_history original
        LEFT JOIN memory_bundle_owned owned ON owned.entry_id = original.id
        WHERE original.source_key = ?
    """,
        (source.encode(),),
    ).fetchone()
    return (row[0], bool(row[1])) if row else None


def _apply_projection(
    connection: sqlite3.Connection, installation_id: str, projection: BundleProjection
) -> int:
    new_count = 0
    for source, record in projection.records.items():
        existing = _entry_id_with_ownership(connection, source)
        if existing is None:
            put_history_record(connection, record)
            entry_id = connection.execute(
                "SELECT id FROM memory_history WHERE source_key = ?", (source.encode(),)
            ).fetchone()[0]
            connection.execute("INSERT INTO memory_bundle_owned VALUES (?)", (entry_id,))
            new_count += 1
        else:
            entry_id = existing[0]
        connection.execute(
            "INSERT INTO memory_bundle_members VALUES (?, ?)", (installation_id, entry_id)
        )
        graph_item = projection.graph.get(source)
        if graph_item is not None and (existing is None or existing[1]):
            if existing is not None:
                row = connection.execute(
                    "SELECT evidence FROM memory_graph_sources WHERE source_key = ?",
                    (source.encode(),),
                ).fetchone()
                if row is not None:
                    saved = json.loads(row["evidence"])
                    known = {bundle_digest(link) for link in saved}
                    links = (asdict(link) for link in graph_item.evidence)
                    saved.extend(link for link in links if bundle_digest(link) not in known)
                    connection.execute(
                        "UPDATE memory_graph_sources SET evidence = ? WHERE source_key = ?",
                        (json.dumps(saved, ensure_ascii=False), source.encode()),
                    )
                    continue
            put_graph_sources(connection, (graph_item,))
    _refresh_removed(connection, installation_id)
    return new_count


def _refresh_removed(connection: sqlite3.Connection, installation_id: str) -> None:
    connection.execute(
        """
        INSERT INTO memory_edits(kind, entry_id, is_bundle_removed)
        SELECT 'history', member.entry_id, NOT EXISTS (
            SELECT 1 FROM memory_bundle_members other
            JOIN memory_bundles owner USING (installation_id)
            WHERE other.entry_id = member.entry_id AND owner.installed
        )
        FROM memory_bundle_members member JOIN memory_bundle_owned owned USING (entry_id)
        WHERE member.installation_id = ?
        ON CONFLICT(kind, entry_id) DO UPDATE SET is_bundle_removed = excluded.is_bundle_removed
    """,
        (installation_id,),
    )


def install_bundle(
    connection: sqlite3.Connection, file: BinaryIO, mode: BundleMode, now: datetime
) -> dict[str, Any]:
    """预检相同资料，再在同一写事务检查目标、登记归属并接入原件。"""
    bundle = load_bundle(file)
    projection = project_bundle(bundle)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        report = _inspect(connection, bundle, projection)
        if mode == "restore" and report["restore_blocked"]:
            raise MemoryBundleConflictError(report["restore_blocked"])
        if report["status"] == "installed":
            return report | {"added": 0, "already_installed": True}
        if report["status"] == "uninstalled":
            raise MemoryBundleConflictError("该包已卸载，请从安装记录恢复。")
        timestamp = now.isoformat()
        connection.execute(
            "INSERT INTO memory_bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                bundle.installation_id,
                bundle.package_id,
                bundle.version,
                bundle.name,
                bundle.content_level,
                bundle.digest,
                timestamp,
                timestamp,
                json.dumps(bundle.manifest, ensure_ascii=False),
                json.dumps(bundle.knowledge, ensure_ascii=False),
                json.dumps(bundle.state, ensure_ascii=False) if bundle.state is not None else None,
            ),
        )
        added = _apply_projection(connection, bundle.installation_id, projection)
        return report | {"status": "installed", "added": added, "already_installed": False}


def list_bundles(connection: sqlite3.Connection, offset: int, limit: int) -> list[dict[str, Any]]:
    """按安装时间倒序读安装记录，包括已经卸载的包。"""
    return [
        dict(row)
        for row in connection.execute(
            """
        SELECT installation_id, package_id, version, name, content_level,
               installed_at, changed_at, installed
        FROM memory_bundles ORDER BY installed_at DESC, installation_id LIMIT ? OFFSET ?
    """,
            (limit, offset),
        )
    ]


def set_bundle_installed(
    connection: sqlite3.Connection, installation_id: str, installed: bool, now: datetime
) -> bool:
    """只更改包的可用状态；独占原件不删除，共享原件不失效。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        result = connection.execute(
            """
            UPDATE memory_bundles SET installed = ?, changed_at = ?
            WHERE installation_id = ? AND installed != ?
        """,
            (installed, now.isoformat(), installation_id, installed),
        )
        if result.rowcount == 0:
            return _installation(connection, installation_id) is not None
        _refresh_removed(connection, installation_id)
        return True


def export_bundle(connection: sqlite3.Connection, installation_id: str, output: BinaryIO) -> bool:
    """下载此安装保存的包原件，不导出本机自学材料。"""
    row = _installation(connection, installation_id)
    if row is None:
        return False
    bundle = MemoryBundle(
        row["package_id"],
        row["version"],
        row["name"],
        row["content_level"],
        row["digest"],
        json.loads(row["manifest"]),
        json.loads(row["knowledge"]),
        json.loads(row["state"]) if row["state"] is not None else None,
    )
    write_bundle(bundle, output)
    return True
