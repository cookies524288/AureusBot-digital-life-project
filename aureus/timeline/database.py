"""时间线数据库。

SQLite 只追加表，通过触发器（trigger）在数据库层面禁止 UPDATE 和 DELETE。
metadata 存成 JSON 文本（D41 第 9 条）。
"""

import ast
import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from aureus.common import Clock, SystemClock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import enable_wal, open_connection
from aureus.timeline.models import ActorType, TimelineQuery, TimelineRecord, VisibilityScope
from aureus.timeline.platform_history import LegacyPlatformHistory

logger = logging.getLogger(__name__)

# 旧记录的 metadata 实在读不出来时，原文放在这个键下面，不丢、也不报错（D41 第 9 条）。
UNREADABLE_METADATA_KEY = "unreadable_metadata"


def _encode_metadata(metadata: dict[str, Any]) -> str:
    """把 metadata 编码成 JSON 文本。

    Raises:
        ValueError: metadata 里有 JSON 表示不了的值（比如 datetime），要由调用方先转成字符串。
    """
    try:
        # ensure_ascii=False：中文按原样存，用 SQLite 工具直接查看时也读得懂。
        return json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"时间线的 metadata 只能放 JSON 能表示的值：{error}") from None


def _decode_metadata(raw: str) -> dict[str, Any]:
    """读回 metadata。兼容改成 JSON 之前用 str() 写进去的旧记录。"""
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        try:
            # literal_eval 只认字面量（字符串、数字、列表、字典……），不会执行代码，
            # 不像原来的 eval()。
            decoded = ast.literal_eval(raw)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            decoded = None
    if not isinstance(decoded, dict):
        logger.warning(
            "时间线里有一条记录的 metadata 读不出来，原文保留在 %s 里", UNREADABLE_METADATA_KEY
        )
        return {UNREADABLE_METADATA_KEY: raw}
    return decoded


class TimelineDatabase:
    """时间线数据库：只追加表。

    只能插入和查询，不能修改、不能删除，通过 SQLite 触发器强制保证。
    """

    def __init__(self, db_path: Path, clock: Clock | None = None):
        """初始化时间线数据库。

        Args:
            db_path: 数据库文件路径
            clock: 时钟对象，默认使用真实时钟
        """
        self._db_path = db_path
        self._clock = clock or SystemClock()
        self._conn: sqlite3.Connection | None = None
        self.platform_history = LegacyPlatformHistory(db_path)

    def open(self) -> None:
        """打开数据库连接并初始化表结构。"""
        if self._conn is not None:
            return

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = open_connection(self._db_path)
        try:
            # 主库启动时最先由这里打开；WAL 让网页读和心跳写不再互相等锁（D121）。
            enable_wal(self._conn)
        except sqlite3.Error:
            logger.exception("把数据库切换到 WAL 模式失败，启动中止")
            self._conn.close()
            self._conn = None
            raise
        self._init_schema()

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _init_schema(self) -> None:
        """初始化数据库表和触发器（禁止 UPDATE 和 DELETE）。"""
        assert self._conn is not None

        # 创建时间线表
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)

        # 创建索引
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeline_timestamp ON timeline(timestamp)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeline_event_type ON timeline(event_type)
        """)

        # 禁止 UPDATE：尝试时抛出错误
        self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS no_update_timeline
            BEFORE UPDATE ON timeline
            BEGIN
                SELECT RAISE(ABORT, 'Timeline records are immutable: UPDATE is not allowed');
            END
        """)

        # 禁止 DELETE：尝试时抛出错误
        self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS no_delete_timeline
            BEFORE DELETE ON timeline
            BEGIN
                SELECT RAISE(ABORT, 'Timeline records are immutable: DELETE is not allowed');
            END
        """)

        # 禁止 INSERT OR REPLACE：它会先删掉冲突的行，而默认情况下这个删除不触发
        # DELETE 触发器，所以要在插入前拦住「主键已存在」（审查 S3）。
        self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS no_replace_timeline
            BEFORE INSERT ON timeline
            WHEN EXISTS (SELECT 1 FROM timeline WHERE id = NEW.id)
            BEGIN
                SELECT RAISE(ABORT, 'Timeline records are immutable: REPLACE is not allowed');
            END
        """)

        self._conn.commit()

    def append(
        self,
        event_type: str,
        content: str,
        actor_type: ActorType,
        actor_id: str = "",
        visibility: VisibilityScope = VisibilityScope.PRIVATE,
        metadata: dict | None = None,
    ) -> TimelineRecord:
        """追加一条时间线记录。

        Args:
            event_type: 事件类型
            content: 事件内容
            actor_type: 执行者类型
            actor_id: 执行者 ID（actor_type=me 时留空）
            visibility: 可见范围
            metadata: 额外元数据

        Returns:
            追加后的时间线记录

        Raises:
            RuntimeError: 数据库未打开
        """
        if self._conn is None:
            raise RuntimeError("Database is not open")

        # 统一成 UTC 再存：查询按字符串比较时间，混进别的时区会比错（与账本一致）。
        timestamp = ensure_aware(self._clock.now())
        metadata_json = _encode_metadata(metadata or {})

        cursor = self._conn.execute(
            """
            INSERT INTO timeline
            (timestamp, actor_type, actor_id, event_type, content, visibility, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp.isoformat(),
                actor_type.value,
                actor_id,
                event_type,
                content,
                visibility.value,
                metadata_json,
            ),
        )
        self._conn.commit()

        return TimelineRecord(
            id=cursor.lastrowid or 0,
            timestamp=timestamp,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=event_type,
            content=content,
            visibility=visibility,
            metadata=metadata or {},
        )

    def query(self, query: TimelineQuery) -> Sequence[TimelineRecord]:
        """查询时间线记录。

        Args:
            query: 查询条件

        Returns:
            匹配的时间线记录列表（按 ID 升序）。query.latest 为 True 时，
            取的是符合条件的最新 limit 条，结果仍按 ID 升序。

        Raises:
            RuntimeError: 数据库未打开
        """
        if self._conn is None:
            raise RuntimeError("Database is not open")

        conditions: list[str] = ["id > ?"]
        params: list[str | int | float | None] = [query.since_id]

        if query.before_id is not None:
            conditions.append("id < ?")
            params.append(query.before_id)

        if query.since_time is not None:
            conditions.append("timestamp >= ?")
            params.append(ensure_aware(query.since_time).isoformat())

        if query.event_type:
            conditions.append("event_type = ?")
            params.append(query.event_type)
        if query.event_types:
            placeholders = ",".join("?" for _ in query.event_types)
            conditions.append(f"event_type IN ({placeholders})")
            params.extend(query.event_types)

        if query.actor_type is not None:
            conditions.append("actor_type = ?")
            params.append(query.actor_type.value)

        if query.visible_to_current_user:
            # 用户可见：user_only、group 或 public
            conditions.append("visibility IN ('user_only', 'group', 'public')")

        if query.visibility is not None:
            conditions.append("visibility = ?")
            params.append(query.visibility.value)

        for key, value in query.metadata.items():
            # 用 JSON 的类型和值一起比，不能把 true、1 和字符串 "true" 当成同一事实。
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each(timeline.metadata) AS actual,"
                " json_each(?) AS wanted WHERE actual.key = wanted.key"
                " AND actual.type = wanted.type AND actual.value IS wanted.value)"
            )
            params.append(_encode_metadata({key: value}))
        for key in query.without_metadata:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM json_each(timeline.metadata) WHERE key = ?)"
            )
            params.append(key)

        where_clause = " AND ".join(conditions)
        order = "DESC" if query.latest else "ASC"
        sql = f"SELECT * FROM timeline WHERE {where_clause} ORDER BY id {order} LIMIT ?"
        params.append(query.limit)

        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        if query.latest:
            rows.reverse()

        return [
            TimelineRecord(
                id=row["id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                actor_type=ActorType(row["actor_type"]),
                actor_id=row["actor_id"],
                event_type=row["event_type"],
                content=row["content"],
                visibility=VisibilityScope(row["visibility"]),
                metadata=_decode_metadata(row["metadata"]),
            )
            for row in rows
        ]

    def get(
        self, record_id: int, *, connection: sqlite3.Connection | None = None
    ) -> TimelineRecord | None:
        """按序号取一条记录；没有则返回 None，数据库未打开时抛出 RuntimeError。"""
        database = connection if connection is not None else self._conn
        if database is None:
            raise RuntimeError("Database is not open")
        row = database.execute("SELECT * FROM timeline WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        return TimelineRecord(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            actor_type=ActorType(row["actor_type"]),
            actor_id=row["actor_id"],
            event_type=row["event_type"],
            content=row["content"],
            visibility=VisibilityScope(row["visibility"]),
            metadata=_decode_metadata(row["metadata"]),
        )

    def get_latest_id(self) -> int:
        """获取当前最大的记录 ID。

        Returns:
            最大的记录 ID，如果为空返回 0
        """
        if self._conn is None:
            raise RuntimeError("Database is not open")

        cursor = self._conn.execute("SELECT MAX(id) as max_id FROM timeline")
        row = cursor.fetchone()
        return row["max_id"] or 0

    def count_records(self) -> int:
        """统计时间线记录总数。

        Returns:
            时间线记录总数
        """
        if self._conn is None:
            raise RuntimeError("Database is not open")

        cursor = self._conn.execute("SELECT COUNT(*) as count FROM timeline")
        row = cursor.fetchone()
        return row["count"]
