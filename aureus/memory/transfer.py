"""长期记忆原件和维护覆盖的 UTF-8 JSONL 交换格式。"""

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from aureus.common.sqlite import open_connection
from aureus.memory.edits import MAX_MEMORY_TEXT
from aureus.memory.history import (
    MEDIA_ASSET_TABLES,
    HistoricalMemory,
    MemoryEvidence,
    MemorySource,
    MemoryStatus,
    SourceValue,
    decode_history_row,
    put_history_record,
    read_history,
)

_ARCHIVE_VERSION = 1
ARCHIVE_HEADER = {"format": "aureus-memory", "version": _ARCHIVE_VERSION}
ARCHIVE_MEDIA_TYPE = "application/x-ndjson"
_ARCHIVE_CATEGORIES = ("messages", "paragraphs", "relations", "fact_claims", "episodes", "other")


class MemoryArchiveError(ValueError):
    """记忆文件缺失、损坏或含有不兼容的记录。"""


class MemoryArchiveConflictError(MemoryArchiveError):
    """文件中的原件或维护覆盖与本库同来源的记录不同。"""


class _ArchiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class _ArchiveSource(_ArchiveModel):
    instance: str = Field(min_length=1)
    role: str = Field(min_length=1)
    table: str = Field(min_length=1)
    key: tuple[SourceValue, ...] = Field(min_length=1)

    def source(self) -> MemorySource:
        return MemorySource(self.instance, self.role, self.table, self.key)


class _ArchiveEvidence(_ArchiveModel):
    kind: str = Field(min_length=1)
    reference: str
    source: _ArchiveSource | None
    status: MemoryStatus | None

    def evidence(self) -> MemoryEvidence:
        return MemoryEvidence(
            self.kind, self.reference, self.source.source() if self.source else None, self.status
        )


class _ArchiveRecord(_ArchiveModel):
    source: _ArchiveSource
    text: str
    actor_label: str
    person_key: str | None
    recorded_at: AwareDatetime | None
    time_precision: str | None
    status: MemoryStatus
    valid_from: AwareDatetime | None
    valid_to: AwareDatetime | None
    evidence: tuple[_ArchiveEvidence, ...]
    received_at: AwareDatetime | None
    edited_text: str | None = Field(min_length=1, max_length=MAX_MEMORY_TEXT)
    is_enabled: bool

    def record(self) -> HistoricalMemory:
        return HistoricalMemory(
            source=self.source.source(),
            text=self.text,
            actor_label=self.actor_label,
            person_key=self.person_key,
            recorded_at=self.recorded_at,
            time_precision=self.time_precision,
            status=self.status,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            evidence=tuple(item.evidence() for item in self.evidence),
            received_at=self.received_at,
        )


def _line(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def export_archive(db_path: Path, output: BinaryIO) -> None:
    """将文字原件和维护覆盖从同一次查询写入已打开的导出文件。"""
    output.write(_line(ARCHIVE_HEADER))
    count = 0
    conn = open_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT history.*, edit.edited_content, coalesce(edit.is_enabled, 1) AS is_enabled
               FROM memory_history history LEFT JOIN memory_edits edit
                 ON edit.kind = 'history' AND edit.entry_id = history.id
               WHERE json_extract(source_key, '$[2]') NOT IN (?, ?, ?, ?)
                 AND NOT coalesce(edit.is_removed, 0)
               ORDER BY history.id""",
            MEDIA_ASSET_TABLES,
        )
        for row in rows:
            record = decode_history_row(row)
            values = asdict(record)
            for key in ("recorded_at", "valid_from", "valid_to", "received_at"):
                stamp = getattr(record, key)
                values[key] = stamp.isoformat() if stamp is not None else None
            for key in ("entry_id", "is_recallable", "original_text"):
                del values[key]
            values["edited_text"] = row["edited_content"]
            values["is_enabled"] = bool(row["is_enabled"])
            output.write(_line(values))
            count += 1
    finally:
        conn.close()
    output.write(_line({"records": count}))


def _parse_archive(file: BinaryIO) -> Iterator[tuple[int, _ArchiveRecord]]:
    try:
        header = json.loads(file.readline().decode("utf-8"))
    except ValueError as error:
        raise MemoryArchiveError("无法读取记忆包文件头。") from error
    if header != ARCHIVE_HEADER or type(header["version"]) is not int:
        raise MemoryArchiveError("记忆包格式或版本不受支持。")
    for line_number, line in enumerate(file, 2):
        try:
            value = json.loads(line.decode("utf-8"))
            if type(value) is dict and "records" in value:
                if (
                    value != {"records": line_number - 2}
                    or type(value["records"]) is not int
                    or file.read(1)
                ):
                    raise MemoryArchiveError("记忆包记录数或结束行有误。")
                return
            entry = _ArchiveRecord.model_validate_json(line)
        except MemoryArchiveError:
            raise
        except ValueError as error:
            raise MemoryArchiveError(f"记忆包第 {line_number} 行格式有误。") from error
        if entry.source.table in MEDIA_ASSET_TABLES:
            raise MemoryArchiveError(f"记忆包第 {line_number} 行包含媒体资产记录。")
        yield line_number, entry
    raise MemoryArchiveError("记忆包缺少结束行，文件可能不完整。")


def import_archive(db_path: Path, file: BinaryIO) -> tuple[int, int]:
    """原子追加文字原件和覆盖，已有相同条目跳过，遇到冲突整批回滚。"""
    conn = open_connection(db_path)
    added = skipped = 0
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for line_number, entry in _parse_archive(file):
                record = entry.record()
                current = read_history(conn, record.source)
                if current is not None:
                    edit = conn.execute(
                        """SELECT edited_content, coalesce(edit.is_enabled, 1) AS is_enabled
                           FROM memory_history history LEFT JOIN memory_edits edit
                             ON edit.kind = 'history' AND edit.entry_id = history.id
                           WHERE source_key = ?""",
                        (record.source.encode(),),
                    ).fetchone()
                    if (
                        current != record
                        or edit["edited_content"] != entry.edited_text
                        or bool(edit["is_enabled"]) != entry.is_enabled
                    ):
                        raise MemoryArchiveConflictError(
                            f"记忆包第 {line_number} 行与现有同来源记录冲突。"
                        )
                    skipped += 1
                    continue
                put_history_record(conn, record)
                if entry.edited_text is not None or not entry.is_enabled:
                    conn.execute(
                        """INSERT INTO memory_edits(kind, entry_id, edited_content, is_enabled)
                           SELECT 'history', id, ?, ? FROM memory_history WHERE source_key = ?""",
                        (entry.edited_text, entry.is_enabled, record.source.encode()),
                    )
                added += 1
    finally:
        conn.close()
    return added, skipped


def inspect_archive(file: BinaryIO) -> dict[str, int | dict[str, int]]:
    """从头到尾校验并按来源表计数，不访问本地库；坏包抛出 MemoryArchiveError。"""
    counts: dict[str, int] = dict.fromkeys(_ARCHIVE_CATEGORIES, 0)
    for _, entry in _parse_archive(file):
        table = entry.source.table
        category = "messages" if table == "mai_messages" else table
        counts[category if category in counts else "other"] += 1
    return {"version": _ARCHIVE_VERSION, "total": sum(counts.values()), "counts": counts}
