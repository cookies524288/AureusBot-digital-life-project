"""MaiBot 导入暂存库：事务写入业务行和可取回的完整快照。"""

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from aureus.tools.migrate.maibot import open_readonly_database
from aureus.tools.migrate.maibot_catalog import STAGING_APPLICATION_ID, MigrationError, Table
from aureus.tools.migrate.maibot_package import (
    BLOCK_SIZE,
    Package,
    Source,
    open_package,
    publish_file,
    require_new_output,
    verify_records,
)

_STAGING_VERSION = 1
_APPLICATION_ID_OFFSET = 68  # SQLite 数据库头中的 4 字节 application_id。
_STAGING_HEADER = STAGING_APPLICATION_ID.to_bytes(4, "big")
_ROW_BATCH_SIZE = 512
_SCHEMA = """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    instance TEXT NOT NULL,
    role TEXT NOT NULL,
    manifest TEXT NOT NULL,
    UNIQUE (instance, role)
);
CREATE TABLE source_rows (
    source_id INTEGER NOT NULL REFERENCES sources(id),
    table_name TEXT NOT NULL,
    source_key TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (source_id, table_name, source_key)
) WITHOUT ROWID;
CREATE TABLE snapshot_blocks (
    source_id INTEGER NOT NULL REFERENCES sources(id),
    block_number INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (source_id, block_number)
);
"""


def _check_target_file(target: Path) -> None:
    # 用文件头拒绝普通 SQLite，避免以可写模式访问运行库或源库。
    with target.open("rb") as stream:
        header = stream.read(_APPLICATION_ID_OFFSET + len(_STAGING_HEADER))
    if not header.startswith(b"SQLite format 3\x00") or (
        header[_APPLICATION_ID_OFFSET:] != _STAGING_HEADER
    ):
        raise MigrationError("not_staging_database")


def _check_target_version(database: sqlite3.Connection) -> None:
    if database.execute("PRAGMA user_version").fetchone()[0] != _STAGING_VERSION:
        raise MigrationError("unsupported_staging_format")


@contextmanager
def open_staging(target: Path) -> Iterator[sqlite3.Connection]:
    """打开已识别版本的暂存库，所有查询共享同一个只读事务。"""
    _check_target_file(target)
    with closing(open_readonly_database(target)) as database:
        database.execute("BEGIN")
        _check_target_version(database)
        yield database


def _write_snapshot(database: sqlite3.Connection, source_id: int, snapshot: Path) -> None:
    with snapshot.open("rb") as stream:
        blocks = iter(lambda: stream.read(BLOCK_SIZE), b"")
        database.executemany(
            "INSERT INTO snapshot_blocks VALUES (?, ?, ?)",
            ((source_id, number, block) for number, block in enumerate(blocks)),
        )


class _RowBatch:
    def __init__(self, database: sqlite3.Connection, source_id: int) -> None:
        self.database = database
        self.source_id = source_id
        self.rows: list[tuple[int, str, str, str]] = []
        self.size = 0

    def add(self, table: Table, key: bytes, line: bytes) -> None:
        if self.size + len(key) + len(line) > BLOCK_SIZE:
            self.flush()
        self.rows.append((self.source_id, table.name, key.decode("ascii"), line.decode("ascii")))
        self.size += len(key) + len(line)
        if len(self.rows) >= _ROW_BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        self.database.executemany("INSERT INTO source_rows VALUES (?, ?, ?, ?)", self.rows)
        self.rows.clear()
        self.size = 0


def _write_package(
    connection: sqlite3.Connection, source: Source, path: Path, work_parent: Path
) -> dict[str, object]:
    with open_package(source, path, work_parent) as opened:
        _, snapshot, manifest, _ = opened
        previous = connection.execute(
            "SELECT manifest FROM sources WHERE instance = ? AND role = ?",
            (source.instance, source.role),
        ).fetchone()
        manifest_text = manifest.decode("ascii")
        if previous is not None:
            if previous[0] != manifest_text:
                raise MigrationError("snapshot_conflict")
            return verify_records(source, opened).report("import", already_exists=True)
        source_id: int = connection.execute(
            "INSERT INTO sources(instance, role, manifest) VALUES (?, ?, ?) RETURNING id",
            (source.instance, source.role, manifest_text),
        ).fetchone()[0]
        batch = _RowBatch(connection, source_id)
        package: Package = verify_records(source, opened, batch.add)
        batch.flush()
        _write_snapshot(connection, source_id, snapshot)
        return package.report("import")


def _import_into(source: Source, path: Path, target: Path, *, create: bool) -> dict[str, object]:
    mode = "rwc" if create else "rw"
    uri = f"{target.absolute().as_uri()}?mode={mode}"
    with closing(sqlite3.connect(uri, uri=True, autocommit=True)) as database:
        database.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False)
        database.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("BEGIN IMMEDIATE")
        if create:
            database.execute(f"PRAGMA application_id = {STAGING_APPLICATION_ID}")
            database.execute(f"PRAGMA user_version = {_STAGING_VERSION}")
            database.executescript(_SCHEMA)
        else:
            _check_target_version(database)
        # 连接关闭会回滚未提交事务。快照与业务行共享一个提交点。
        report = _write_package(database, source, path, target.parent)
        database.execute("COMMIT")
        return report


def import_package(source: Source, path: Path, target: Path) -> dict[str, object]:
    """校验并导入到独立暂存库。同来源不同快照须使用新目标。"""
    if target.exists() or target.is_symlink():
        _check_target_file(target)
        return _import_into(source, path, target, create=False)
    with tempfile.TemporaryDirectory(dir=target.parent) as work:
        temporary = Path(work) / "staging.sqlite3"
        temporary.touch(mode=0o600, exist_ok=False)
        report = _import_into(source, path, temporary, create=True)
        publish_file(temporary, target)
        return report


@dataclass(frozen=True)
class StagedSource:
    """同一只读事务中的暂存清单与业务行。"""

    database: sqlite3.Connection
    source_id: int
    manifest: str

    def rows(self, table: str) -> Iterator[tuple[str, dict[str, object]]]:
        """按原主键数组返回对应业务行的原值字典。"""
        for key, record in self.database.execute(
            "SELECT source_key, record FROM source_rows WHERE source_id = ? AND table_name = ?",
            (self.source_id, table),
        ):
            yield key, json.loads(record)["values"]

    def source_counts(self) -> dict[str, int]:
        """读取原始条数，包括暂存层未导出的行。"""
        return {
            item["table"]["name"].casefold(): item["source"]
            for item in json.loads(self.manifest)["tables"]
        }


@contextmanager
def read_staged_source(source: Source, target: Path) -> Iterator[StagedSource]:
    """只读打开正式暂存库中的一个已校验来源。"""
    _check_target_file(target)
    with closing(open_readonly_database(target)) as database:
        database.execute("BEGIN")
        _check_target_version(database)
        row = database.execute(
            "SELECT id, manifest FROM sources WHERE instance = ? AND role = ?",
            (source.instance, source.role),
        ).fetchone()
        if row is None:
            raise MigrationError("source_not_imported")
        yield StagedSource(database, row[0], row[1])


def _restore_snapshot(source: Source, database: sqlite3.Connection, temporary: Path) -> None:
    match = database.execute(
        "SELECT id, json_extract(manifest, '$.snapshot_sha256') FROM sources "
        "WHERE instance = ? AND role = ?",
        (source.instance, source.role),
    ).fetchone()
    if match is None:
        raise MigrationError("source_not_imported")
    source_id, expected_digest = match
    digest = hashlib.sha256()
    with temporary.open("wb") as output:
        for (block,) in database.execute(
            "SELECT data FROM snapshot_blocks WHERE source_id = ? ORDER BY block_number",
            (source_id,),
        ):
            output.write(block)
            digest.update(block)
    if digest.hexdigest() != expected_digest:
        raise MigrationError("snapshot_checksum")


@contextmanager
def open_staged_snapshot(
    source: Source, database: sqlite3.Connection, work_parent: Path
) -> Iterator[sqlite3.Connection]:
    """从同一暂存读事务校验完整快照，在临时目录中只读打开。"""
    with tempfile.TemporaryDirectory(dir=work_parent) as work:
        temporary = Path(work) / "snapshot.sqlite3"
        temporary.touch(mode=0o600, exist_ok=False)
        _restore_snapshot(source, database, temporary)
        with closing(open_readonly_database(temporary)) as snapshot:
            yield snapshot


def restore_snapshot(source: Source, target: Path, output: Path) -> dict[str, object]:
    """从暂存库校验并恢复一个来源快照，不覆盖既有文件。"""
    require_new_output(output)
    with tempfile.TemporaryDirectory(dir=output.parent) as work:
        temporary = Path(work) / "snapshot.sqlite3"
        temporary.touch(mode=0o600, exist_ok=False)
        with open_staging(target) as database:
            _restore_snapshot(source, database, temporary)
        publish_file(temporary, output)
    return {"operation": "restore", "role": source.role, "snapshot_verified": True}
