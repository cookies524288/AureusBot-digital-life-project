"""收件箱图片向量；使用原消息地址关联来源，原始图片由平台保管。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime, timedelta
from math import hypot, sumprod
from pathlib import Path
from sqlite3 import Connection, Row
from sqlite3 import Error as SQLiteError
from typing import Any

from aureus.common import Clock, SystemClock
from aureus.common.sqlite import open_connection
from aureus.heartbeat.inbox_media import AttachmentReader
from aureus.heartbeat.inbox_store import (
    USER_VISIBLE_INBOX_MESSAGE,
    ConversationKey,
    InboxStore,
)
from aureus.heartbeat.perception import MediaReference, Perception
from aureus.organs import OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError

logger = logging.getLogger(__name__)

# 退避曲线：读不到的旧附件不必每拍重试；首次 30 分钟，逐次翻倍，封顶 24 小时（D151）。
_RETRY_BASE_SECONDS = 30 * 60
_RETRY_MAX_SECONDS = 24 * 60 * 60
# 每批扫描比写入条数更多的候选，坏附件才不会挡住后面的可读图片（D151）。
_SCAN_MULTIPLIER = 4
_SCAN_MIN_CANDIDATES = 64
_SCAN_MAX_CANDIDATES = 256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_image_vectors (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES inbox_messages(id),
    attachment TEXT NOT NULL,
    space TEXT NOT NULL,
    vector TEXT NOT NULL,
    UNIQUE (message_id, attachment)
);
CREATE TABLE IF NOT EXISTS inbox_image_retries (
    message_id INTEGER NOT NULL REFERENCES inbox_messages(id),
    attachment TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    retry_after TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (message_id, attachment)
);
"""
_SOURCE = """
    FROM inbox_image_vectors v
    JOIN inbox_messages m ON m.id = v.message_id
    JOIN inbox_conversations c ON c.id = m.conversation_id
"""
_PUBLIC_FIELDS = "v.id, v.attachment, m.id AS message_id, m.timestamp, c.name AS conversation"
_CONVERSATION_KEY = (
    "c.platform = ? AND c.account_id = ? AND c.kind = ? AND c.recipient_id = ? "
    "AND c.temporary_group_id = ?"
)
_COVERAGE = f"""
WITH candidates AS (
    SELECT DISTINCT m.id AS message_id, m.conversation_id, attachment.value ->> '$[1]' AS file_id
    FROM inbox_messages m JOIN json_each(m.attachments) attachment
    WHERE m.direction = 'incoming' AND {USER_VISIBLE_INBOX_MESSAGE}
    AND attachment.type = 'array' AND attachment.value ->> '$[0]' = 'image'
    AND json_type(attachment.value, '$[1]') = 'text' AND attachment.value ->> '$[1]' <> ''
)
SELECT c.platform, c.account_id, c.kind, c.recipient_id, c.temporary_group_id, v.space, COUNT(*)
FROM candidates p JOIN inbox_conversations c ON c.id = p.conversation_id
LEFT JOIN inbox_image_vectors v ON v.message_id = p.message_id AND v.attachment = p.file_id
GROUP BY c.id, v.space
"""
PICTURE_REFERENCES = (
    """
    SELECT 'inbox', v.id, unixepoch(m.timestamp, 'subsec'), c.name, NULL, NULL,
           v.attachment, m.id, c.name, m.timestamp, NULL
"""
    + _SOURCE
)


class InboxImageIndex:
    """在网页资源页中按已阅读的图片查询相似附件。"""

    def __init__(
        self,
        path: Path,
        call: Callable[[OrganRequest], Awaitable[OrganResponse]],
        enabled: Callable[[], bool],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._path, self._call, self._enabled = path, call, enabled
        self._clock = clock or SystemClock()
        # 后台回填持有这一批的写权；此时按需索引跳过，等下一轮回填统一处理。
        self._backfilling = False
        with closing(open_connection(path)) as connection:
            connection.executescript(_SCHEMA)

    @property
    def enabled(self) -> bool:
        """图片嵌入模型当前是否可用。"""
        return self._enabled()

    async def index(self, perception: Perception, attachment: MediaReference, image: bytes) -> None:
        """收到有效图片时保存向量；被网关拒绝或模型失败时保留原消息。"""
        if (
            self._backfilling
            or not self._enabled()
            or perception.route is None
            or attachment.kind != "image"
        ):
            return
        try:
            await self._index(perception, attachment, image)
        except SQLiteError:
            logger.exception("图片向量索引写入失败")

    async def backfill_batch(
        self,
        store: InboxStore,
        read: AttachmentReader,
        allows: Callable[[ConversationKey], bool],
        limit: int,
    ) -> tuple[bool, bool]:
        """按会话权限回填一批已收到但未建索引的图片（D125）。

        每批可以扫描比写入条数更多的候选，跳过读不到的附件继续往后找（D151）。

        Returns:
            （是否还有未建索引的图片，本批是否写入过向量）；本批没有写入任何向量时
            两者都为假，构建器结束本轮，不再连续空转。
        """
        if not self._enabled():
            return False, False
        if self._backfilling:
            return False, False
        self._backfilling = True
        try:
            now = self._clock.now()
            conversations = await asyncio.to_thread(store.visible_conversation_keys)
            remaining = limit
            scanned = min(max(limit * _SCAN_MULTIPLIER, _SCAN_MIN_CANDIDATES), _SCAN_MAX_CANDIDATES)
            written = 0
            skipped = 0
            for conversation in conversations:
                if remaining <= 0 or scanned <= 0:
                    break
                if not allows(conversation):
                    continue
                rows = await asyncio.to_thread(self._unindexed_images, conversation, scanned, now)
                scanned -= len(rows)
                for message_id, position, file_id in rows:
                    if remaining <= 0:
                        break
                    loaded = await asyncio.to_thread(
                        store.get_visible_image, conversation, message_id, position
                    )
                    if loaded is None:
                        await asyncio.to_thread(
                            self._mark_retry, message_id, file_id, "附件不可见", now
                        )
                        skipped += 1
                        continue
                    perception, attachment, _note = loaded
                    media = await read(perception, attachment)
                    if media is None:
                        await asyncio.to_thread(
                            self._mark_retry, message_id, file_id, "读取附件失败", now
                        )
                        skipped += 1
                        continue
                    data, _mime = media
                    try:
                        saved = await self._index(
                            perception,
                            attachment,
                            data,
                            propagate_rejection=True,
                            stop_on_circuit=True,
                        )
                    except OrganCallError:
                        # 服务商正在熔断冷却：本批立即停下，下一拍再试（D150、D151）。
                        return False, written > 0
                    except SQLiteError:
                        # 数据库本身不可用，写不了退避记录，下一拍再试即可（D151）。
                        logger.exception("图片向量索引写入失败")
                        skipped += 1
                        continue
                    if saved:
                        written += 1
                        remaining -= 1
                    else:
                        await asyncio.to_thread(
                            self._mark_retry, message_id, file_id, "未保存向量", now
                        )
                        skipped += 1
            if skipped:
                logger.debug("图片索引本轮跳过 %s 条暂不可用的附件", skipped)
            return written > 0, written > 0
        finally:
            self._backfilling = False

    def _unindexed_images(
        self, conversation: ConversationKey, limit: int, now: datetime
    ) -> list[tuple[int, int, str]]:
        """该会话中仍随可见入站消息保存、尚未建索引、也不在退避期内的图片。

        Returns:
            （消息序号，附件位置，附件地址）的列表。
        """
        stamp = now.isoformat(timespec="microseconds")
        with closing(open_connection(self._path, read_only=True)) as connection:
            rows = connection.execute(
                f"""SELECT m.id, attachment.key, attachment.value ->> '$[1]'
                    FROM inbox_messages m JOIN json_each(m.attachments) attachment
                    WHERE m.direction = 'incoming' AND {USER_VISIBLE_INBOX_MESSAGE}
                    AND attachment.type = 'array' AND attachment.value ->> '$[0]' = 'image'
                    AND json_type(attachment.value, '$[1]') = 'text'
                    AND attachment.value ->> '$[1]' <> ''
                    AND m.conversation_id IN (
                        SELECT c.id FROM inbox_conversations c WHERE {_CONVERSATION_KEY}
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM inbox_image_vectors v
                        WHERE v.message_id = m.id
                        AND v.attachment = attachment.value ->> '$[1]'
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM inbox_image_retries r
                        WHERE r.message_id = m.id
                        AND r.attachment = attachment.value ->> '$[1]'
                        AND r.retry_after > ?
                    )
                    ORDER BY m.id, attachment.key LIMIT ?""",
                (*conversation.parameters(), stamp, limit),
            ).fetchall()
        return [(int(row[0]), int(row[1]), str(row[2])) for row in rows]

    async def _index(
        self,
        perception: Perception,
        attachment: MediaReference,
        image: bytes,
        *,
        propagate_rejection: bool = False,
        stop_on_circuit: bool = False,
    ) -> bool:
        """写入一张图片的向量；返回是否真的保存了。"""
        message_id = await asyncio.to_thread(self._unindexed_source, perception, attachment)
        if message_id is None:
            return False
        try:
            response = await self._call(
                OrganRequest("image_embedding", "", embedding_inputs=(image,))
            )
        except CallRejectedError:
            if propagate_rejection:
                raise
            return False
        except OrganCallError as error:
            if stop_on_circuit and error.skipped_by_circuit:
                raise
            return False
        if len(response.embeddings) != 1 or not isinstance(
            space := response.metadata.get("embedding_space"), str
        ):
            return False
        vector = response.embeddings[0]
        norm = hypot(*vector)
        if not norm:
            return False
        await asyncio.to_thread(
            self._save, message_id, attachment.file_id, space, tuple(x / norm for x in vector)
        )
        return True

    def _unindexed_source(self, perception: Perception, attachment: MediaReference) -> int | None:
        route = perception.route
        assert route is not None
        key = ConversationKey.from_perception(perception)
        with closing(open_connection(self._path)) as connection:
            row = connection.execute(
                """SELECT m.id FROM inbox_messages m
                   JOIN inbox_conversations c ON c.id = m.conversation_id
                   WHERE c.platform = ? AND c.account_id = ? AND c.kind = ?
                   AND c.recipient_id = ? AND c.temporary_group_id = ? AND m.message_id = ?
                   AND NOT EXISTS (SELECT 1 FROM inbox_image_vectors v
                                   WHERE v.message_id = m.id AND v.attachment = ?)""",
                (*key.parameters(), route.message_id, attachment.file_id),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def _mark_retry(self, message_id: int, attachment: str, reason: str, now: datetime) -> None:
        """记录一次读不到或没保存，并按次数延长退避（D151）。"""
        with closing(open_connection(self._path)) as connection, connection:
            row = connection.execute(
                "SELECT attempts FROM inbox_image_retries WHERE message_id=? AND attachment=?",
                (message_id, attachment),
            ).fetchone()
            attempts = (int(row["attempts"]) if row is not None else 0) + 1
            delay = min(_RETRY_BASE_SECONDS * 2 ** (attempts - 1), _RETRY_MAX_SECONDS)
            retry_after = (now + timedelta(seconds=delay)).isoformat(timespec="microseconds")
            connection.execute(
                "INSERT INTO inbox_image_retries(message_id, attachment, attempts,"
                " retry_after, reason) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(message_id, attachment) DO UPDATE SET"
                " attempts=excluded.attempts, retry_after=excluded.retry_after,"
                " reason=excluded.reason",
                (message_id, attachment, attempts, retry_after, reason),
            )

    def _save(
        self, message_id: int, attachment: str, space: str, vector: tuple[float, ...]
    ) -> None:
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO inbox_image_vectors(message_id, attachment, space, vector)"
                " SELECT id, ?, ?, ? FROM inbox_messages WHERE id=?",
                (attachment, space, json.dumps(vector), message_id),
            )
            # 写成功说明附件又读得到了，退避记录随之失效（D151）。
            connection.execute(
                "DELETE FROM inbox_image_retries WHERE message_id=? AND attachment=?",
                (message_id, attachment),
            )

    @staticmethod
    def retention_vectors(connection: Connection, ids: str, *, remove: bool = False) -> int:
        """统计或清理这些原件专有的向量；不触碰全局表情目录和其他原件。"""
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='inbox_image_vectors'"
            ).fetchone()
            is None
        ):
            return 0
        scope = "FROM inbox_image_vectors WHERE message_id IN (SELECT value FROM json_each(?))"
        if remove:
            return connection.execute("DELETE " + scope, (ids,)).rowcount
        return int(connection.execute("SELECT COUNT(*) " + scope, (ids,)).fetchone()[0])

    def browse(self, before: int | None, limit: int) -> list[dict[str, Any]]:
        """按收录顺序显示来源会话、消息时间和附件引用。"""
        rows, _total = self.browse_page(before, limit)
        return rows

    def coverage_by_conversation(self) -> list[tuple[ConversationKey, str | None, int]]:
        """只读统计仍随可见入站消息保存的图片；调用方必须再检查会话路由。"""
        with closing(open_connection(self._path, read_only=True)) as connection:
            rows = connection.execute(_COVERAGE)
            return [
                (
                    ConversationKey(platform, account, kind, recipient, temporary or None),
                    space,
                    total,
                )
                for platform, account, kind, recipient, temporary, space, total in rows
            ]

    def browse_page(self, before: int | None, limit: int) -> tuple[list[dict[str, Any]], int]:
        """返回收件箱图片当前页和不受翻页游标影响的总条数。"""
        with closing(open_connection(self._path)) as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT count(*) " + _SOURCE).fetchone()[0]
            rows = connection.execute(
                "SELECT "
                + _PUBLIC_FIELDS
                + _SOURCE
                + " WHERE (? IS NULL OR v.id < ?) ORDER BY v.id DESC LIMIT ?",
                (before, before, limit),
            )
            return [dict(row) for row in rows], total

    def similar(self, source_id: int, limit: int) -> list[dict[str, Any]] | None:
        """按余弦相似度保存前几位，只比较来自同一模型空间的向量。"""
        from heapq import nlargest

        with closing(open_connection(self._path)) as connection:
            source = connection.execute(
                "SELECT v.vector, v.space " + _SOURCE + " WHERE v.id = ?", (source_id,)
            ).fetchone()
            if source is None:
                return None
            query = json.loads(source["vector"])
            rows = connection.execute(
                "SELECT "
                + _PUBLIC_FIELDS
                + ", v.vector "
                + _SOURCE
                + " WHERE v.space = ? AND v.id <> ?",
                (source["space"], source_id),
            )

            def scored(row: Row) -> tuple[float, dict[str, Any]] | None:
                vector = json.loads(row["vector"])
                if len(vector) != len(query):
                    return None
                details = dict(row)
                del details["vector"]
                return sumprod(query, vector), details

            matches = (match for row in rows if (match := scored(row)) is not None)
            return [
                dict(details, score=score)
                for score, details in nlargest(limit, matches, key=lambda item: item[0])
            ]
