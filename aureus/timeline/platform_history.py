"""按原始读权和精确路由读取旧时间线消息，不改写时间线。"""

import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Row

from aureus.common.sqlite import open_connection

from .models import ActorType, TimelineRecord, VisibilityScope

_METADATA = "CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END"
_BASE = f"""
    SELECT id, timestamp, actor_type, actor_id, event_type, content, visibility,
           {_METADATA} AS details
    FROM timeline WHERE event_type = 'message' AND visibility IN ('user_only', 'group', 'public')
"""
_PLATFORM_ROWS = f"""
WITH candidates AS ({_BASE}), routed AS (
    SELECT *, details ->> '$.channel' AS platform,
           details ->> '$.account_id' AS account, details ->> '$.message_type' AS kind,
           details ->> '$.recipient_id' AS recipient, details ->> '$.temporary_group_id' AS temp,
           details ->> '$.message_id' AS message_key
    FROM candidates WHERE details ->> '$.channel' IN ('qq', 'web', 'terminal')
), classified AS (
    SELECT *, CASE WHEN platform = 'qq' AND typeof(account) = 'text' AND account != ''
        AND typeof(recipient) = 'text' AND recipient != ''
        AND (
            (kind = 'group' AND temp IS NULL)
            OR (kind = 'private' AND (
                json_type(details, '$.temporary_group_id') = 'null'
                OR (typeof(temp) = 'text' AND temp != '')
            ))
        ) THEN 1 ELSE 0 END AS has_route
    FROM routed WHERE details ->> '$.inbox_message_id' IS NULL
)
"""

type LegacyRoute = tuple[str, str, str, str, str | None]


def _record(row: Row) -> TimelineRecord:
    details = json.loads(row["details"])
    timestamp = datetime.fromisoformat(row["timestamp"])
    source = details.get("source_timestamp")
    if type(source) is int:
        try:
            timestamp = datetime.fromtimestamp(source, UTC)
        except (OSError, OverflowError, ValueError):
            pass
    return TimelineRecord(
        id=row["id"],
        timestamp=timestamp,
        actor_type=ActorType(row["actor_type"]),
        actor_id=row["actor_id"],
        event_type=row["event_type"],
        content=row["content"],
        visibility=VisibilityScope(row["visibility"]),
        metadata=details,
    )


class LegacyPlatformHistory:
    """从时间线提供有边界的旧消息页和原路由目录。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def routes(self) -> list[tuple[LegacyRoute, int, datetime]]:
        """列出有完整路由的旧 QQ 会话，不含未知路由。"""
        with closing(open_connection(self._path)) as database:
            rows = database.execute(f"""
                {_PLATFORM_ROWS}
                SELECT account, kind, recipient, temp, MAX(id) AS id, timestamp
                FROM classified WHERE has_route
                GROUP BY account, kind, recipient, temp ORDER BY id DESC
            """).fetchall()
        return [
            (
                ("qq", row["account"], row["kind"], row["recipient"], row["temp"]),
                row["id"],
                datetime.fromisoformat(row["timestamp"]),
            )
            for row in rows
        ]

    def page(
        self,
        route: LegacyRoute | None,
        *,
        before_id: int | None,
        limit: int,
        archive_account_id: str | None = None,
    ) -> list[TimelineRecord]:
        """按追加序号倒序读取精确原路由或显式未归类的独立页。"""
        if route is None:
            scope = "NOT has_route AND (account IS NULL OR account = ?)"
            parameters: tuple[str | None, ...] = (archive_account_id,)
        elif route[0] == "qq":
            scope = "has_route AND account = ? AND kind = ? AND recipient = ? AND temp IS ?"
            parameters = route[1:]
        else:
            return []
        distinct = "" if route is None else f"""
            AND (typeof(message_key) != 'text' OR message_key = '' OR id IN (
                SELECT MIN(id) FROM classified WHERE {scope} GROUP BY message_key))
        """
        distinct_parameters = () if route is None else parameters
        with closing(open_connection(self._path)) as database:
            rows = database.execute(
                f"""{_PLATFORM_ROWS}
                SELECT * FROM classified WHERE {scope} {distinct} AND (? IS NULL OR id < ?)
                ORDER BY id DESC LIMIT ?""",
                (*parameters, *distinct_parameters, before_id, before_id, limit),
            ).fetchall()
        return [_record(row) for row in rows]
