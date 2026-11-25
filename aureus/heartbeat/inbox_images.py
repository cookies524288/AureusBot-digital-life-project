"""收件箱图片向量；使用原消息地址关联来源，原始图片由平台保管。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import closing
from math import hypot, sumprod
from pathlib import Path
from sqlite3 import Connection, Row
from sqlite3 import Error as SQLiteError
from typing import Any

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_image_vectors (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES inbox_messages(id),
    attachment TEXT NOT NULL,
    space TEXT NOT NULL,
    vector TEXT NOT NULL,
    UNIQUE (message_id, attachment)
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
    ) -> None:
        self._path, self._call, self._enabled = path, call, enabled
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

        Returns:
            （是否还有未建索引的图片，本批是否写入过向量）。
        """
        if not self._enabled():
            return False, False
        if self._backfilling:
            return True, False
        self._backfilling = True
        try:
            conversations = await asyncio.to_thread(store.visible_conversation_keys)
            remaining = limit
            written = 0
            skipped = 0
            for conversation in conversations:
                if remaining <= 0:
                    break
                if not allows(conversation):
                    continue
                rows = await asyncio.to_thread(self._unindexed_images, conversation, remaining)
                for message_id, position in rows:
                    loaded = await asyncio.to_thread(
                        store.get_visible_image, conversation, message_id, position
                    )
                    if loaded is None:
                        skipped += 1
                        continue
                    perception, attachment, _note = loaded
                    media = await read(perception, attachment)
                    if media is None:
                        skipped += 1
                        continue
                    data, _mime = media
                    try:
                        saved = await self._index(
                            perception, attachment, data, propagate_rejection=True
                        )
                    except SQLiteError:
                        logger.exception("图片向量索引写入失败")
                        saved = False
                    if saved:
                        written += 1
                        remaining -= 1
                    else:
                        skipped += 1
            if skipped:
                logger.debug("图片索引本轮跳过 %s 条暂不可用的附件", skipped)
            return remaining == 0 or skipped > 0, written > 0
        finally:
            self._backfilling = False

    def _unindexed_images(
        self, conversation: ConversationKey, limit: int
    ) -> list[tuple[int, int]]:
        """该会话中仍随可见入站消息保存、尚未建索引的图片（消息序号，附件位置）。"""
        with closing(open_connection(self._path, read_only=True)) as connection:
            rows = connection.execute(
                f"""SELECT m.id, attachment.key
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
                    ORDER BY m.id, attachment.key LIMIT ?""",
                (*conversation.parameters(), limit),
            ).fetchall()
        return [(int(row[0]), int(row[1])) for row in rows]

    async def _index(
        self,
        perception: Perception,
        attachment: MediaReference,
        image: bytes,
        *,
        propagate_rejection: bool = False,
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
        except OrganCallError:
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

    def _save(
        self, message_id: int, attachment: str, space: str, vector: tuple[float, ...]
    ) -> None:
        with closing(open_connection(self._path)) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO inbox_image_vectors(message_id, attachment, space, vector)"
                " SELECT id, ?, ?, ? FROM inbox_messages WHERE id=?",
                (attachment, space, json.dumps(vector), message_id),
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
