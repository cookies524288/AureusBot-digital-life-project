"""时间线数据库。

SQLite 只追加表，通过触发器（trigger）在数据库层面禁止 UPDATE 和 DELETE。
metadata 存成 JSON 文本（D41 第 9 条）。
"""

import ast
import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any

from aureus.common import Clock, SystemClock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import enable_wal, open_connection
from aureus.timeline.models import ActorType, TimelineQuery, TimelineRecord, VisibilityScope
from aureus.timeline.platform_history import LegacyPlatformHistory

logger = logging.getLogger(__name__)

# 旧记录的 metadata 实在读不出来时，原文放在这个键下面，不丢、也不报错（D41 第 9 条）。
UNREADABLE_METADATA_KEY = "unreadable_metadata"
_READ_WORKERS = 2


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
        self._open = False
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._writer: ThreadPoolExecutor | None = None
        self._readers: ThreadPoolExecutor | None = None
        self._append_listeners: list[Callable[[TimelineRecord], None]] = []
        self._notification_lock = threading.Lock()
        self._listener_loop: asyncio.AbstractEventLoop | None = None
        self._notifications: SimpleQueue[TimelineRecord] = SimpleQueue()
        self.platform_history = LegacyPlatformHistory(db_path)

    def open(self) -> None:
        """打开数据库连接并初始化表结构。"""
        if self._open:
            return

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_connection(self._db_path)
        try:
            # 主库启动时最先由这里打开；WAL 让网页读和心跳写不再互相等锁（D121）。
            enable_wal(connection)
            self._init_schema(connection)
        except BaseException:
            connection.close()
            raise
        self._local.connection = connection
        self._connections.append(connection)
        self._writer = ThreadPoolExecutor(1, thread_name_prefix="aureus-timeline-write")
        self._readers = ThreadPoolExecutor(_READ_WORKERS, thread_name_prefix="aureus-timeline-read")
        with contextlib.suppress(RuntimeError):
            self._listener_loop = asyncio.get_running_loop()
        self._open = True

    def close(self) -> None:
        """等所有已提交的操作完成，再回收各线程的连接。"""
        writer, self._writer = self._writer, None
        readers, self._readers = self._readers, None
        if writer is not None:
            writer.shutdown()
        if readers is not None:
            readers.shutdown()
        with self._connections_lock:
            self._open = False
            for connection in self._connections:
                connection.close()
            self._connections.clear()
            self._local = threading.local()
        self._listener_loop = None

    def _connection(self, *, read_only: bool = True) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        with self._connections_lock:
            if not self._open:
                raise RuntimeError("Database is not open")
            connection = open_connection(self._db_path, read_only=read_only)
            self._local.connection = connection
            self._connections.append(connection)
        return connection

    def _init_schema(self, connection: sqlite3.Connection) -> None:
        """初始化数据库表和触发器（禁止 UPDATE 和 DELETE）。"""
        # 创建时间线表
        connection.execute("""
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
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeline_timestamp ON timeline(timestamp)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeline_event_type ON timeline(event_type)
        """)
        # 收件箱去重按 metadata 里的入箱序号查；表达式索引让每条消息不再全表扫（D129、D137）。
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_timeline_message_inbox_id
            ON timeline(json_extract(metadata, '$.inbox_message_id'))
            WHERE event_type = 'message'
        """)

        # 禁止 UPDATE：尝试时抛出错误
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS no_update_timeline
            BEFORE UPDATE ON timeline
            BEGIN
                SELECT RAISE(ABORT, 'Timeline records are immutable: UPDATE is not allowed');
            END
        """)

        # 禁止 DELETE：尝试时抛出错误
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS no_delete_timeline
            BEFORE DELETE ON timeline
            BEGIN
                SELECT RAISE(ABORT, 'Timeline records are immutable: DELETE is not allowed');
            END
        """)

        # 禁止 INSERT OR REPLACE：它会先删掉冲突的行，而默认情况下这个删除不触发
        # DELETE 触发器，所以要在插入前拦住「主键已存在」（审查 S3）。
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS no_replace_timeline
            BEFORE INSERT ON timeline
            WHEN EXISTS (SELECT 1 FROM timeline WHERE id = NEW.id)
            BEGIN
                SELECT RAISE(ABORT, 'Timeline records are immutable: REPLACE is not allowed');
            END
        """)

        connection.commit()

    def add_append_listener(self, listener: Callable[[TimelineRecord], None]) -> Callable[[], None]:
        """登记追加钩子：每次成功追加后收到刚写入的那条记录。

        钩子只读：记录已经提交，时间线只追加、不能改（D11）。钩子用来通知别的子系统
        （比如网页实时推送），它出错只记日志，不影响追加本身。

        Returns:
            取消登记的函数；重复调用没有影响。
        """
        self._append_listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._append_listeners.remove(listener)

        return remove

    def append(
        self,
        event_type: str,
        content: str,
        actor_type: ActorType,
        actor_id: str = "",
        visibility: VisibilityScope = VisibilityScope.PRIVATE,
        metadata: dict | None = None,
    ) -> TimelineRecord:
        """同步追加一条时间线记录，供启动和 CLI 使用。"""
        writer = self._writer
        if writer is None:
            raise RuntimeError("Database is not open")
        current_loop = None
        with contextlib.suppress(RuntimeError):
            current_loop = asyncio.get_running_loop()
        listener_loop = self._get_listener_loop(current_loop)
        record = writer.submit(
            self._write_record,
            listener_loop,
            ensure_aware(self._clock.now()),
            event_type,
            content,
            actor_type,
            actor_id,
            visibility,
            metadata,
        ).result()
        if listener_loop is current_loop:
            self._dispatch_notifications()
        return record

    async def async_append(
        self,
        event_type: str,
        content: str,
        actor_type: ActorType,
        actor_id: str = "",
        visibility: VisibilityScope = VisibilityScope.PRIVATE,
        metadata: dict | None = None,
    ) -> TimelineRecord:
        """异步追加；同一实例的全部追加由单写线程按提交顺序执行。"""
        writer = self._writer
        if writer is None:
            raise RuntimeError("Database is not open")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            writer,
            self._write_record,
            self._get_listener_loop(loop),
            ensure_aware(self._clock.now()),
            event_type,
            content,
            actor_type,
            actor_id,
            visibility,
            metadata,
        )

    def _get_listener_loop(
        self, current_loop: asyncio.AbstractEventLoop | None
    ) -> asyncio.AbstractEventLoop | None:
        if self._listener_loop is None or not self._listener_loop.is_running():
            self._listener_loop = current_loop
        return self._listener_loop

    def _write_record(
        self,
        listener_loop: asyncio.AbstractEventLoop | None,
        timestamp: datetime,
        event_type: str,
        content: str,
        actor_type: ActorType,
        actor_id: str,
        visibility: VisibilityScope,
        metadata: dict | None,
    ) -> TimelineRecord:
        metadata_json = _encode_metadata(metadata or {})
        connection = self._connection(read_only=False)
        with connection:
            cursor = connection.execute(
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

        record = TimelineRecord(
            id=cursor.lastrowid or 0,
            timestamp=timestamp,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=event_type,
            content=content,
            visibility=visibility,
            metadata=metadata or {},
        )
        self._notifications.put(record)
        if listener_loop is not None and listener_loop.is_running():
            try:
                listener_loop.call_soon_threadsafe(self._dispatch_notifications)
            except RuntimeError:
                if listener_loop.is_running():
                    raise
        return record

    def _dispatch_notifications(self) -> None:
        with self._notification_lock:
            while True:
                try:
                    record = self._notifications.get_nowait()
                except Empty:
                    return
                for listener in list(self._append_listeners):
                    try:
                        listener(record)
                    except Exception:
                        logger.exception("时间线追加钩子出错，不影响这条记录")

    async def async_query(self, query: TimelineQuery) -> list[TimelineRecord]:
        """在固定大小的只读线程池查询；繁忙时排队，不丢查询。"""
        return await self.async_read(self.query, query)

    async def async_read[T, **P](
        self, read: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        """将一组只读时间线操作放到同一个读工作线程。"""
        readers = self._readers
        if readers is None:
            raise RuntimeError("Database is not open")
        return await asyncio.get_running_loop().run_in_executor(
            readers, partial(read, *args, **kwargs)
        )

    def query(self, query: TimelineQuery) -> list[TimelineRecord]:
        """查询时间线记录。

        Args:
            query: 查询条件

        Returns:
            匹配的时间线记录列表（按 ID 升序）。query.latest 为 True 时，
            取的是符合条件的最新 limit 条，结果仍按 ID 升序。

        Raises:
            RuntimeError: 数据库未打开
        """
        connection = self._connection()

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

        cursor = connection.execute(sql, params)
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
        database = connection if connection is not None else self._connection()
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

    def inbox_message_ids(self, ids: Iterable[int]) -> set[int]:
        """批量查出这些收件箱序号里已经记进时间线的那些，供收件箱分页去重。"""
        encoded = json.dumps(sorted(set(ids)))
        rows = (
            self._connection()
            .execute(
                "SELECT DISTINCT CAST(json_extract(metadata, '$.inbox_message_id') AS INTEGER) "
                "FROM timeline WHERE event_type = 'message' "
                "AND json_extract(metadata, '$.inbox_message_id') "
                "IN (SELECT value FROM json_each(?))",
                (encoded,),
            )
            .fetchall()
        )
        return {int(row[0]) for row in rows}

    def get_latest_id(self) -> int:
        """获取当前最大的记录 ID。

        Returns:
            最大的记录 ID，如果为空返回 0
        """
        cursor = self._connection().execute("SELECT MAX(id) as max_id FROM timeline")
        row = cursor.fetchone()
        return row["max_id"] or 0

    def count_records(self) -> int:
        """统计时间线记录总数。

        Returns:
            时间线记录总数
        """
        cursor = self._connection().execute("SELECT COUNT(*) as count FROM timeline")
        row = cursor.fetchone()
        return row["count"]
