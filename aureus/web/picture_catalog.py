"""同一只读快照中的归档图片和平台附件引用。"""

from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aureus.common.sqlite import open_connection
from aureus.heartbeat.inbox_images import PICTURE_REFERENCES
from aureus.memory.history import MemorySource
from aureus.memory.resources import PICTURE_ARCHIVE


def browse_pictures(
    database: Path,
    archived: bool,
    indexed: bool,
    query: str,
    availability: str,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """按原图可用情况筛选，在同一个读取快照查询计数和页面。"""
    projections = []
    if archived:
        projections.append(PICTURE_ARCHIVE)
    if indexed:
        projections.append(PICTURE_REFERENCES)
    if not projections:
        return [], 0
    combined = " UNION ALL ".join(projections)
    filtered = f"""
        WITH catalog(kind, id, happened, text, source_key, status, attachment, message_id,
                     conversation, timestamp, digest) AS ({combined}),
        matching AS (SELECT * FROM catalog
            WHERE (:availability = '' OR (digest IS NOT NULL) = (:availability = 'archived'))
              AND (:query = '' OR instr(lower(text), lower(:query)) > 0
                              OR instr(lower(source_key), lower(:query)) > 0
                              OR instr(lower(attachment), lower(:query)) > 0))
    """
    params = {"query": query, "availability": availability, "offset": offset, "limit": limit}
    with closing(open_connection(database)) as connection, connection:
        connection.execute("BEGIN")
        total = connection.execute(filtered + "SELECT count(*) FROM matching", params).fetchone()[0]
        rows = connection.execute(
            filtered + "SELECT * FROM matching ORDER BY happened DESC NULLS LAST, kind, id DESC"
            " LIMIT :limit OFFSET :offset",
            params,
        )
        items = []
        for row in rows:
            item = dict(row)
            source, happened = item.pop("source_key"), item.pop("happened")
            item["source"] = asdict(MemorySource.decode(source)) if source else None
            item["timestamp"] = item["timestamp"] or (
                datetime.fromtimestamp(happened, UTC) if happened is not None else None
            )
            items.append(item)
        return items, total
