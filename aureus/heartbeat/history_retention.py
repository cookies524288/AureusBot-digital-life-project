"""当前平台会话的旧历史保留策略；预览减法与认证流程分离。"""

import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from hashlib import sha256
from sqlite3 import Connection
from threading import Lock
from typing import Literal
from uuid import uuid4

from aureus.common import Clock
from aureus.common.sqlite import open_connection
from aureus.timeline import VisibilityScope
from aureus.timeline.platform_history import LegacyPlatformHistory

from .inbox_images import InboxImageIndex
from .inbox_store import ConversationKey, InboxStore

_PROPOSAL_TTL = timedelta(minutes=30)
_MAX_PENDING = 32
_VISIBLE = frozenset({VisibilityScope.USER_ONLY, VisibilityScope.GROUP, VisibilityScope.PUBLIC})


@dataclass(frozen=True)
class CleanupScope:
    """由服务端固定的会话、可见范围和独占截止时间。"""

    key: ConversationKey
    visibility: VisibilityScope
    cutoff: datetime


@dataclass(frozen=True)
class CleanupPreview:
    """不含消息正文的可核对范围及原件、旧记录和向量数量。"""

    id: str
    scope: CleanupScope
    expires_at: datetime
    read_water: int
    inbox_water: int
    timeline_water: int
    originals: int
    timeline_only: int
    image_vectors: int
    fingerprint: str

    def summary(self) -> dict[str, str | int]:
        """返回网页及模型所需的范围计数；不返回内部指纹。"""
        return {
            "id": self.id,
            "cutoff": self.scope.cutoff.isoformat(),
            "visibility": self.scope.visibility.value,
            "read_water": self.read_water,
            "inbox_water": self.inbox_water,
            "timeline_water": self.timeline_water,
            "originals": self.originals,
            "timeline_only": self.timeline_only,
            "image_vectors": self.image_vectors,
        }


@dataclass(frozen=True)
class _Candidates:
    originals: tuple[tuple[int, str], ...]
    legacy: tuple[tuple[int, str | None], ...]
    vectors: int

    def inbox_ids(self) -> str:
        return json.dumps([identifier for identifier, _ in self.originals])

    def fingerprint(self) -> str:
        return sha256(json.dumps((self.originals, self.legacy, self.vectors)).encode()).hexdigest()


class HistoryRetention:
    """预览无副作用；心跳接受后才发表，已认证确认在事务中重新复核。"""

    def __init__(
        self,
        store: InboxStore,
        legacy: LegacyPlatformHistory,
        clock: Clock,
        timezone: tzinfo | None,
    ) -> None:
        self._store, self._legacy, self._clock, self._timezone = (
            store,
            legacy,
            clock,
            timezone or UTC,
        )
        self._pending: dict[ConversationKey, CleanupPreview] = {}
        self._pending_lock = Lock()

    def available(self) -> bool:
        """同一个 SQLite 数据库才能原子复核本地来源。"""
        return self._legacy.shares_database(self._store.database_path)

    def preview(
        self,
        key: ConversationKey,
        visibility: VisibilityScope,
        cutoff: date,
    ) -> CleanupPreview | None:
        """截止日期自身不包含，按运行实例时区转换为确定的时间。"""
        if not self.available() or key.platform != "qq" or visibility not in _VISIBLE:
            raise ValueError("当前会话没有可用的历史清理范围")
        now = self._clock.now()
        if cutoff > now.astimezone(self._timezone).date():
            return None
        scope = CleanupScope(key, visibility, datetime.combine(cutoff, time.min, self._timezone))
        with closing(open_connection(self._store.database_path, read_only=True)) as connection:
            connection.execute("BEGIN")
            read_water = self._read_water(connection, key)
            inbox_water = int(
                connection.execute(
                    "SELECT high_water FROM inbox_message_sequence WHERE id=1"
                ).fetchone()[0]
            )
            timeline_water = self._legacy.high_water(connection)
            candidates = self._candidates(
                connection, scope, read_water, inbox_water, timeline_water
            )
        return CleanupPreview(
            uuid4().hex,
            scope,
            now + _PROPOSAL_TTL,
            read_water,
            inbox_water,
            timeline_water,
            len(candidates.originals),
            len(candidates.legacy),
            candidates.vectors,
            candidates.fingerprint(),
        )

    def publish(self, preview: CleanupPreview) -> None:
        """只发布当前未被抢占的轮次所接受的预览；每会话至多一份。"""
        with self._pending_lock:
            self._pending.pop(preview.scope.key, None)
            if len(self._pending) >= _MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
            self._pending[preview.scope.key] = preview

    def pending(self, key: ConversationKey) -> CleanupPreview | None:
        """给能看该会话的维护者取得未过期的预览，不读取正文。"""
        with self._pending_lock:
            preview = self._pending.get(key)
        return preview if preview and preview.expires_at > self._clock.now() else None

    def confirm(
        self,
        preview: CleanupPreview,
        permits: Callable[[], bool],
    ) -> Literal["completed", "changed", "denied"]:
        """独占事务复核候选、当前读水位及授权；不接受客户端自定义水位。"""
        scope = preview.scope
        with closing(open_connection(self._store.database_path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not permits():
                return "denied"
            if (
                self.pending(scope.key) != preview
                or not (preview.originals or preview.timeline_only)
                or self._read_water(connection, scope.key) != preview.read_water
                or connection.execute(
                    "SELECT 1 FROM inbox_history_cleanup_audit WHERE proposal_id=?",
                    (preview.id,),
                ).fetchone()
                is not None
            ):
                return "changed"
            candidates = self._candidates(
                connection,
                scope,
                preview.read_water,
                preview.inbox_water,
                preview.timeline_water,
            )
            if candidates.fingerprint() != preview.fingerprint:
                return "changed"
            if not permits():
                return "denied"
            if not self._claim(preview):
                return "changed"
            self._remove(connection, preview, candidates)
        return "completed"

    def forget(self, preview: CleanupPreview) -> None:
        """清除已确认或已失效的预览，不误删同会话刚发布的另一份。"""
        with self._pending_lock:
            if self._pending.get(preview.scope.key) == preview:
                self._pending.pop(preview.scope.key)

    def _claim(self, preview: CleanupPreview) -> bool:
        key = preview.scope.key
        with self._pending_lock:
            if self._pending.get(key) != preview or preview.expires_at <= self._clock.now():
                return False
            self._pending.pop(key)
            return True

    @staticmethod
    def _read_water(connection: Connection, key: ConversationKey) -> int:
        row = connection.execute(
            """SELECT last_read_id FROM inbox_conversations WHERE platform=? AND account_id=?
                AND kind=? AND recipient_id=? AND temporary_group_id=?""",
            key.parameters(),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def _candidates(
        self,
        connection: Connection,
        scope: CleanupScope,
        read_water: int,
        inbox_water: int,
        timeline_water: int,
    ) -> _Candidates:
        key = scope.key
        cutoff_epoch = scope.cutoff.timestamp()
        originals = tuple(
            (row[0], row[1])
            for row in connection.execute(
                """SELECT m.id, m.message_id FROM inbox_messages m JOIN inbox_conversations c
                ON c.id=m.conversation_id WHERE c.platform=? AND c.account_id=? AND c.kind=?
                AND c.recipient_id=? AND c.temporary_group_id=? AND m.visibility=?
                AND ((m.direction='incoming' AND m.id<=?) OR m.direction='outgoing')
                AND m.id<=? AND unixepoch(m.timestamp,'subsec')<?
                AND unixepoch(COALESCE(m.received_at,
                    CASE WHEN m.source_timestamp IS NULL THEN m.timestamp END),'subsec')<?
                ORDER BY m.id""",
                (
                    *key.parameters(),
                    scope.visibility,
                    read_water,
                    inbox_water,
                    cutoff_epoch,
                    cutoff_epoch,
                ),
            )
        )
        legacy = self._legacy.retention_candidates(
            connection,
            (key.platform, key.account_id, key.kind, key.recipient_id, key.temporary_group_id),
            scope.visibility,
            cutoff_epoch,
            timeline_water,
        )
        linked_now = self._present_platform_ids(connection, key, legacy)
        hidden = {
            row[0]
            for row in connection.execute(
                "SELECT source_key FROM inbox_history_exclusions "
                "WHERE scope=? AND source='timeline'",
                (key.language_scope_key(),),
            )
        }
        remaining = tuple(
            item for item in legacy if str(item[0]) not in hidden and item[1] not in linked_now
        )
        selected = _Candidates(originals, remaining, 0)
        return _Candidates(
            originals,
            remaining,
            InboxImageIndex.retention_vectors(connection, selected.inbox_ids()),
        )

    @staticmethod
    def _present_platform_ids(
        connection: Connection,
        key: ConversationKey,
        legacy: list[tuple[int, str | None]],
    ) -> set[str]:
        if not legacy:
            return set()
        return {
            row[0]
            for row in connection.execute(
                """SELECT m.message_id FROM inbox_messages m JOIN inbox_conversations c
                ON c.id=m.conversation_id WHERE c.platform=? AND c.account_id=? AND c.kind=?
                AND c.recipient_id=? AND c.temporary_group_id=?
                AND m.message_id IN (SELECT value FROM json_each(?))
                UNION SELECT source_key FROM inbox_history_exclusions
                WHERE scope=? AND source='platform'""",
                (
                    *key.parameters(),
                    json.dumps([identifier for _, identifier in legacy if identifier]),
                    key.language_scope_key(),
                ),
            )
        }

    def _remove(
        self, connection: Connection, preview: CleanupPreview, candidates: _Candidates
    ) -> None:
        scope = preview.scope
        key = scope.key.language_scope_key()
        exclusions = [(key, "platform", identifier) for _, identifier in candidates.originals]
        for identifier, platform_id in candidates.legacy:
            exclusions.append((key, "timeline", str(identifier)))
            if platform_id:
                exclusions.append((key, "platform", platform_id))
        connection.executemany(
            "INSERT OR IGNORE INTO inbox_history_exclusions VALUES (?, ?, ?)", exclusions
        )
        InboxImageIndex.retention_vectors(connection, candidates.inbox_ids(), remove=True)
        connection.execute(
            "DELETE FROM inbox_messages WHERE id IN (SELECT value FROM json_each(?))",
            (candidates.inbox_ids(),),
        )
        connection.execute(
            """INSERT INTO inbox_history_cleanup_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                preview.id,
                key,
                scope.visibility,
                scope.cutoff.isoformat(),
                preview.read_water,
                preview.inbox_water,
                preview.timeline_water,
                preview.originals,
                preview.timeline_only,
                preview.image_vectors,
                self._clock.now().isoformat(),
            ),
        )
