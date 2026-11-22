"""MaiBot 私有迁移包的一致快照、流式导出和校验。"""

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO

from aureus.tools.migrate.maibot import open_readonly_database
from aureus.tools.migrate.maibot_catalog import (
    BUSINESS_TABLES,
    MigrationError,
    Table,
    count_rows,
    encode_json,
    encode_row,
    read_rows,
    read_tables,
)

FORMAT_VERSION = 1
SNAPSHOT = "source.sqlite3"
RECORDS = "records.jsonl"
MANIFEST = "manifest.json"
BLOCK_SIZE = 4 * 1024 * 1024
_BACKUP_PAGES = 1024
_MANIFEST_LIMIT = 4 * 1024 * 1024
_FOLLOW_UP = {
    "external_files_and_media": "resolve_external_refs_and_scan_encoded_media",
    "vectors": "rebuild_after_publication",
    "plugin_runtime": "retain_in_snapshot_and_review_manually",
    "unknown_tables": "retain_in_snapshot_and_review_schema",
}

type RowSink = Callable[[Table, bytes, bytes], None]


@dataclass(frozen=True)
class Source:
    """由调用方指定、与源路径无关的迁移来源。"""

    instance: str
    role: str

    def __post_init__(self) -> None:
        """校验来源参数，诊断不输出用户传入的值。"""
        if not self.instance.strip() or self.role not in BUSINESS_TABLES:
            raise MigrationError("invalid_source")


@dataclass(frozen=True)
class TableCount:
    """保存一张源表的导出、隔离与跳过数量。"""

    table: Table
    source: int
    exported: int
    quarantined: int = 0


@dataclass(frozen=True)
class Package:
    """验证所得的快照清单和各表计数。"""

    source: Source
    user_version: int
    snapshot_sha256: str
    records_sha256: str
    counts: tuple[TableCount, ...]

    def manifest(self) -> bytes:
        """生成含私有来源键和表结构的完整清单。"""
        return encode_json(
            {
                "format": "aureus.maibot",
                "version": FORMAT_VERSION,
                "source": asdict(self.source),
                "snapshot_scope": "single_database_at_read_transaction_start",
                "user_version": self.user_version,
                "snapshot_sha256": self.snapshot_sha256,
                "records_sha256": self.records_sha256,
                "tables": [asdict(count) for count in self.counts],
                "follow_up": _FOLLOW_UP,
            }
        )

    def report(self, operation: str, *, already_exists: bool = False) -> dict[str, object]:
        """输出角色和逐表计数。未知表只以私有清单中的编号定位。"""
        tables = []
        for count in self.counts:
            table = count.table
            item = {
                "table": table.number,
                "category": table.category,
                "reason": table.reason,
                "source": count.source,
                "exported": count.exported,
                "received": count.exported if operation == "import" and not already_exists else 0,
                "already_exists": count.exported if already_exists else 0,
                "quarantined": count.quarantined,
                "skipped": count.source - count.exported - count.quarantined,
            }
            if table.name.casefold() in BUSINESS_TABLES[self.source.role]:
                item["name"] = table.name.casefold()
            if count.quarantined:
                item["quarantine_reason"] = "null_primary_key"
            tables.append(item)
        return {
            "operation": operation,
            "format_version": FORMAT_VERSION,
            "role": self.source.role,
            "user_version": self.user_version,
            "tables": tables,
            "follow_up": _FOLLOW_UP,
        }


def copy_and_hash(source: IO[bytes], target: IO[bytes]) -> str:
    """以固定大小复制数据并返回 SHA-256。"""
    digest = hashlib.sha256()
    while block := source.read(BLOCK_SIZE):
        target.write(block)
        digest.update(block)
    return digest.hexdigest()


def require_new_output(output: Path) -> None:
    """拒绝覆盖已有文件、目录或符号链接。"""
    if output.exists() or output.is_symlink():
        raise MigrationError("output_exists")


def publish_file(temporary: Path, output: Path) -> None:
    """在同一卷原子发布完整文件，同时保护其他进程已创建的文件。"""
    try:
        os.link(temporary, output)
    except FileExistsError:
        raise MigrationError("output_exists") from None


def _backup(source: Path, snapshot: Path) -> None:
    with (
        closing(open_readonly_database(source)) as reader,
        closing(sqlite3.connect(snapshot, autocommit=True)) as destination,
    ):
        reader.execute("BEGIN")
        # BEGIN 延迟获取快照，先读取库头以固定已提交的 WAL 边界。
        reader.execute("PRAGMA main.user_version").fetchone()
        reader.backup(destination, pages=_BACKUP_PAGES)
        destination.execute("PRAGMA journal_mode = DELETE").fetchone()


def _scan_table(database: sqlite3.Connection, table: Table, sink: RowSink) -> TableCount:
    if table.category != "export":
        return TableCount(table, count_rows(database, table), 0)
    exported = quarantined = 0
    previous_key = None
    for key, row in read_rows(database, table):
        if None in key:
            quarantined += 1
            continue
        encoded_key, line = encode_row(table, key, row)
        if encoded_key == previous_key:
            raise MigrationError("duplicate_source_key", table=table.number, row=exported + 1)
        sink(table, encoded_key, line)
        previous_key = encoded_key
        exported += 1
    return TableCount(table, exported + quarantined, exported, quarantined)


def _scan(snapshot: Path, role: str, sink: RowSink) -> tuple[int, tuple[TableCount, ...]]:
    with closing(open_readonly_database(snapshot)) as database:
        # SQLite TEXT 也可能含不合法的 UTF-8。JSON 转义保留这些原始字节。
        database.text_factory = lambda value: value.decode("utf-8", "surrogateescape")
        version: int = database.execute("PRAGMA main.user_version").fetchone()[0]
        counts = tuple(_scan_table(database, table, sink) for table in read_tables(database, role))
    return version, counts


def export_package(source: Source, database: Path, output: Path) -> Package:
    """备份显式源库，再从备份生成并发布独立迁移包。"""
    require_new_output(output)
    with tempfile.TemporaryDirectory(dir=output.parent) as work:
        snapshot = Path(work) / SNAPSHOT
        archive_path = Path(work) / "package.zip"
        archive_path.touch(mode=0o600, exist_ok=False)
        _backup(database, snapshot)
        with zipfile.ZipFile(archive_path, "w") as archive:
            with (
                snapshot.open("rb") as original,
                archive.open(SNAPSHOT, "w", force_zip64=True) as entry,
            ):
                snapshot_digest = copy_and_hash(original, entry)
            records_digest = hashlib.sha256()
            with archive.open(RECORDS, "w", force_zip64=True) as records:

                def write_row(_table: Table, _key: bytes, line: bytes) -> None:
                    records.write(line)
                    records_digest.update(line)

                version, counts = _scan(snapshot, source.role, write_row)
            package = Package(source, version, snapshot_digest, records_digest.hexdigest(), counts)
            archive.writestr(MANIFEST, package.manifest())
        publish_file(archive_path, output)
    return package


def _read_manifest(archive: zipfile.ZipFile, source: Source) -> tuple[bytes, object]:
    if sorted(archive.namelist()) != sorted((SNAPSHOT, RECORDS, MANIFEST)):
        raise MigrationError("invalid_package_members")
    with archive.open(MANIFEST) as entry:
        content = entry.read(_MANIFEST_LIMIT + 1)
    if len(content) > _MANIFEST_LIMIT:
        raise MigrationError("invalid_manifest")
    try:
        manifest = json.loads(content)
    except (ValueError, RecursionError):
        raise MigrationError("invalid_manifest") from None
    if not isinstance(manifest, dict) or manifest.get("format") != "aureus.maibot":
        raise MigrationError("invalid_manifest")
    if type(manifest.get("version")) is not int or manifest["version"] != FORMAT_VERSION:
        raise MigrationError("unsupported_format")
    if manifest.get("source") != asdict(source):
        raise MigrationError("source_mismatch")
    return content, manifest.get("snapshot_sha256")


@contextmanager
def open_package(
    source: Source, path: Path, work_parent: Path
) -> Iterator[tuple[zipfile.ZipFile, Path, bytes, str]]:
    """展开并校验一个完整备份，临时文件仅在上下文内使用。"""
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory(dir=work_parent) as work:
        manifest, expected_digest = _read_manifest(archive, source)
        snapshot = Path(work) / SNAPSHOT
        with archive.open(SNAPSHOT) as entry, snapshot.open("xb") as output:
            digest = copy_and_hash(entry, output)
        if digest != expected_digest:
            raise MigrationError("snapshot_checksum")
        yield archive, snapshot, manifest, digest


def verify_records(
    source: Source,
    opened: tuple[zipfile.ZipFile, Path, bytes, str],
    sink: RowSink | None = None,
) -> Package:
    """逐行同备份比对，确认遗漏、重复、主键和表对账均可检出。"""
    archive, snapshot, manifest, snapshot_digest = opened
    records_digest = hashlib.sha256()
    previous_table = row_number = 0
    with archive.open(RECORDS) as records:

        def verify_row(table: Table, key: bytes, line: bytes) -> None:
            nonlocal previous_table, row_number
            row_number = row_number + 1 if previous_table == table.number else 1
            previous_table = table.number
            if records.readline(len(line) + 1) != line:
                raise MigrationError("record_mismatch", table=table.number, row=row_number)
            records_digest.update(line)
            if sink is not None:
                sink(table, key, line)

        version, counts = _scan(snapshot, source.role, verify_row)
        if records.read(1):
            raise MigrationError("extra_records")
    package = Package(source, version, snapshot_digest, records_digest.hexdigest(), counts)
    if manifest != package.manifest():
        raise MigrationError("manifest_mismatch")
    return package


def preview_package(source: Source, path: Path) -> Package:
    """预演迁移包的全量校验，无需运行或暂存目标。"""
    with open_package(source, path, path.parent) as opened:
        return verify_records(source, opened)
