"""从已迁入的 MaiBot 表情归档恢复原始登记和使用统计。"""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, tzinfo
from pathlib import Path

from aureus.common import AureusPaths, Clock
from aureus.memory.emoji import EmojiStore, ImportedEmoji
from aureus.memory.history import read_history
from aureus.tools.migrate.maibot import open_readonly_database
from aureus.tools.migrate.maibot_catalog import MigrationError
from aureus.tools.migrate.maibot_memory_rows import MemoryRows, StagedValue
from aureus.tools.migrate.maibot_staging import open_staging

_ARCHIVED = re.compile(r"attachments/([0-9a-f]{64})")


def _number(value: StagedValue) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _timestamp(value: StagedValue, zone: tzinfo) -> float | None:
    if not isinstance(value, str):
        return _number(value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)).timestamp()


def publish_maibot_emoji(
    stage_path: Path, instance: str, paths: AureusPaths, clock: Clock, zone: tzinfo
) -> dict[str, object]:
    """仅从暂存表情和已发布归档追加目录，输出汇总计数。"""
    store = EmojiStore(paths.database_file, paths.memory_dir, clock)
    records: list[ImportedEmoji] = []
    verified = 0
    with (
        open_staging(stage_path) as staged,
        closing(open_readonly_database(paths.database_file)) as target,
    ):
        if not target.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'memory_history'"
        ).fetchone():
            raise MigrationError("not_aureus_memory_target")
        target.row_factory = sqlite3.Row
        for row in MemoryRows(staged, instance).rows("main", "images"):
            raw = row.values
            if str(raw.get("image_type")).casefold() != "emoji":
                continue
            existing = read_history(target, row.source)
            digest = (
                next(
                    (
                        match[1]
                        for item in existing.evidence
                        if item.kind == "attachment_archived"
                        and (match := _ARCHIVED.fullmatch(item.reference))
                    ),
                    None,
                )
                if existing
                else None
            )
            if digest is not None and store.read_media(digest) is not None:
                verified += 1
            description = raw.get("description")
            records.append(
                ImportedEmoji(
                    row.source,
                    digest,
                    description if isinstance(description, str) else "",
                    raw.get("is_registered") in (1, "1", "True"),
                    raw.get("is_banned") in (1, "1", "True"),
                    1,
                    max(0, int(_number(raw.get("query_count")) or 0)),
                    _timestamp(raw.get("record_time"), zone),
                    _timestamp(raw.get("last_used_time"), zone),
                    json.dumps(raw, ensure_ascii=False),
                )
            )
    store.open()
    written, assets = store.import_records(records)
    return {
        "operation": "publish-emoji",
        "source_rows": len(records),
        "new_sources": written,
        "new_assets": assets,
        "verified_media": verified,
        "unavailable_media": len(records) - verified,
    }
