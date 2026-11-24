"""只读解释 3.2 暂存库的来源行和对账清单。"""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from typing import cast

from aureus.memory import MemorySource
from aureus.tools.migrate.maibot_catalog import STAGING_APPLICATION_ID, MigrationError

type StagedValue = str | int | float | dict[str, str] | None


@dataclass(frozen=True)
class StagedRow:
    """3.2 行的原主键和原字段。"""

    source: MemorySource
    values: dict[str, StagedValue]


class MemoryRows:
    """同一事务内读取选定逻辑实例的主库和记忆库。"""

    def __init__(self, conn: sqlite3.Connection, instance: str) -> None:
        self.conn = conn
        self.instance = instance
        self.ids: dict[str, int] = {}
        self.tables: dict[tuple[str, str], int] = {}
        self.manifests: dict[str, str] = {}
        if conn.execute("PRAGMA application_id").fetchone()[0] != STAGING_APPLICATION_ID:
            raise MigrationError("not_staging_database")
        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise MigrationError("unsupported_staging_format")
        for source_id, role, manifest in conn.execute(
            "SELECT id, role, manifest FROM sources "
            "WHERE instance = ? AND role IN ('main', 'memory')",
            (instance,),
        ):
            self.ids[role] = source_id
            self.manifests[role] = manifest
            for item in json.loads(manifest)["tables"]:
                table = item["table"]
                if table["category"] == "export":
                    self.tables[role, table["name"].casefold()] = item["exported"]
        if self.ids.keys() != {"main", "memory"}:
            raise MigrationError("memory_source_roles_missing")

    def fingerprint(self, source_timezone: str) -> str:
        """固定独立目标的一组源快照，避免异版本数据混用。"""
        values = (source_timezone, self.manifests["main"], self.manifests["memory"])
        return sha256(json.dumps(values).encode()).hexdigest()

    def bundle_origin(self) -> str:
        """给同一暂存批次的人物和记忆发布提供统一指纹。"""
        return self.fingerprint("bundle-origin-v1")

    def rows(self, role: str, name: str) -> Iterator[StagedRow]:
        """逐条读已识别表，每张表必须与 3.2 的已导出条数一致。"""
        count = 0
        for source_key, record in self.conn.execute(
            "SELECT source_key, record FROM source_rows "
            "WHERE source_id = ? AND table_name = ? ORDER BY source_key",
            (self.ids[role], name),
        ):
            count += 1
            payload = json.loads(record)
            key = json.loads(source_key)
            if key != payload["key"]:
                raise MigrationError("staging_key_mismatch")
            yield StagedRow(
                MemorySource(self.instance, role, name, tuple(key)),
                cast(dict[str, StagedValue], payload["values"]),
            )
        if count != self.tables.get((role, name), 0):
            raise MigrationError("staging_row_count_mismatch")
