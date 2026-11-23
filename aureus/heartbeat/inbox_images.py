"""收件箱图片向量；使用原消息地址关联来源，原始图片由平台保管。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import closing
from math import hypot, sumprod
from pathlib import Path
from sqlite3 import Error as SQLiteError
from sqlite3 import Row
from typing import Any

from aureus.common.sqlite import open_connection
from aureus.heartbeat.inbox_store import ConversationKey
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


class InboxImageIndex:
    """在网页资源页中按已阅读的图片查询相似附件。"""

    def __init__(
        self,
        path: Path,
        call: Callable[[OrganRequest], Awaitable[OrganResponse]],
        enabled: Callable[[], bool],
    ) -> None:
        self._path, self._call, self._enabled = path, call, enabled
        with closing(open_connection(path)) as connection:
            connection.executescript(_SCHEMA)

    async def index(self, perception: Perception, attachment: MediaReference, image: bytes) -> None:
        """收到有效图片时保存向量；被网关拒绝或模型失败时保留原消息。"""
        if not self._enabled() or perception.route is None or attachment.kind != "image":
            return
        try:
            await self._index(perception, attachment, image)
        except SQLiteError:
            logger.exception("图片向量索引写入失败")

    async def _index(
        self, perception: Perception, attachment: MediaReference, image: bytes
    ) -> None:
        message_id = await asyncio.to_thread(self._unindexed_source, perception, attachment)
        if message_id is None:
            return
        try:
            response = await self._call(
                OrganRequest("image_embedding", "", embedding_inputs=(image,))
            )
        except (CallRejectedError, OrganCallError):
            return
        if len(response.embeddings) != 1 or not isinstance(
            space := response.metadata.get("embedding_space"), str
        ):
            return
        vector = response.embeddings[0]
        norm = hypot(*vector)
        if norm:
            await asyncio.to_thread(
                self._save, message_id, attachment.file_id, space, tuple(x / norm for x in vector)
            )

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
                " VALUES (?, ?, ?, ?)",
                (message_id, attachment, space, json.dumps(vector)),
            )

    def browse(self, before: int | None, limit: int) -> list[dict[str, Any]]:
        """按收录顺序显示来源会话、消息时间和附件引用。"""
        with closing(open_connection(self._path)) as connection:
            rows = connection.execute(
                "SELECT "
                + _PUBLIC_FIELDS
                + _SOURCE
                + " WHERE (? IS NULL OR v.id < ?) ORDER BY v.id DESC LIMIT ?",
                (before, before, limit),
            )
            return [dict(row) for row in rows]

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
