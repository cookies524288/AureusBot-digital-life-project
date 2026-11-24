"""把已授权的平台原件适配为总结窗口与只读证据。"""

from collections.abc import Callable
from datetime import datetime
from sqlite3 import Connection

from aureus.common.web_identity import WEB_CHANNEL_NAME, WEB_USER_ID
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.heartbeat.perception import TrustLevel, VisibilityScope
from aureus.memory import MemoryIndex, SummaryMessage, SummaryScope, SummaryWindow
from aureus.timeline import ActorType, TimelineDatabase, TimelineRecord

WEB_PRIVATE_SCOPE: SummaryScope = (f"{WEB_CHANNEL_NAME}:{WEB_USER_ID}", VisibilityScope.USER_ONLY)


def summary_scope_for(
    channel: str,
    sender_id: str,
    trust: TrustLevel,
    visibility: VisibilityScope,
    route_key: str | None,
) -> SummaryScope | None:
    """回复只召回与当前感知绑定的平台事实；网页私聊要求固定的经验证账号。"""
    if route_key is not None and channel == "qq":
        return route_key, visibility
    if (channel, sender_id, trust, visibility) == (
        WEB_CHANNEL_NAME,
        WEB_USER_ID,
        TrustLevel.VERIFIED_USER,
        VisibilityScope.USER_ONLY,
    ) and route_key is None:
        return WEB_PRIVATE_SCOPE
    return None


def _material(message: InboxMessage) -> SummaryMessage:
    perception = message.perception
    route = perception.route
    assert route is not None
    return SummaryMessage(
        message.id,
        route.message_id,
        perception.sender_id,
        message.sender_name,
        perception.content,
        message.direction == "incoming"
        and perception.sender_id != route.account_id
        and perception.trust in (TrustLevel.VERIFIED_USER, TrustLevel.VERIFIED_CONTACT),
        quoted_message_id=perception.quoted_message_id,
        images=tuple(
            str(index)
            for index, item in enumerate(perception.attachments)
            if item.kind == "image" and not item.is_emoji and item.file_id.strip()
        ),
    )


def _web_material(record: TimelineRecord) -> SummaryMessage:
    is_human = record.actor_type is ActorType.USER
    return SummaryMessage(
        record.id,
        f"web:{record.id}",
        WEB_USER_ID if is_human else "bot",
        "网页用户" if is_human else "助手",
        record.content,
        is_human,
    )


class PlatformSummarySource:
    """在取材、写入与召回时复用当前通道范围，不凭历史授权推断。"""

    def __init__(
        self, store: InboxStore | None, index: MemoryIndex, timeline: TimelineDatabase | None = None
    ) -> None:
        self.store = store
        self.index = index
        self.timeline = timeline
        self.allows: Callable[[ConversationKey], bool] | None = None
        self.web_enabled: Callable[[], bool] | None = None

    def next_window(self, size: int) -> SummaryWindow | None:
        """读取达到既有批量门槛的最早已授权单范围窗口。"""
        store, timeline = self.store, self.timeline
        scopes: list[SummaryScope] = (
            [
                (key.language_scope_key(), visibility)
                for key, visibility in store.summary_scopes()
                if self.allows is not None and self.allows(key)
            ]
            if store is not None and self.allows is not None
            else []
        )
        if timeline is not None and self.allows_scope(WEB_PRIVATE_SCOPE):
            scopes.append(WEB_PRIVATE_SCOPE)
        earliest: tuple[datetime, SummaryWindow] | None = None
        for scope in scopes:
            start, summaries, facts = self.index.summary_reference(scope)
            messages, timestamp = self._window(scope, start, size)
            if (
                len(messages) == size
                and timestamp is not None
                and (earliest is None or timestamp < earliest[0])
            ):
                earliest = timestamp, SummaryWindow(scope, start, messages, summaries, facts)
        if earliest is not None and self.allows_scope(earliest[1].scope):
            return earliest[1]
        return None

    def allows_scope(self, scope: SummaryScope) -> bool:
        """授权随通道改变，不能以曾经成功的窗口代替。"""
        if scope == WEB_PRIVATE_SCOPE:
            return self.timeline is not None and self.web_enabled is not None and self.web_enabled()
        if self.allows is None or self.store is None or not scope[0].startswith("["):
            return False
        return self.allows(ConversationKey.from_scope_key(scope[0]))

    def _window(
        self,
        scope: SummaryScope,
        start: int,
        size: int,
        *,
        connection: Connection | None = None,
    ) -> tuple[tuple[SummaryMessage, ...], datetime | None]:
        if not self.allows_scope(scope):
            return (), None
        if scope == WEB_PRIVATE_SCOPE and self.timeline is not None:
            records = self.timeline.platform_history.web_private_window(
                start,
                size,
                connection=connection,
            )
            return tuple(map(_web_material, records)), records[0].timestamp if records else None
        if self.store is None:
            return (), None
        route, visibility = scope
        messages = self.store.summary_window(
            ConversationKey.from_scope_key(route),
            VisibilityScope(visibility),
            start,
            size,
            connection=connection,
        )
        return tuple(map(_material, messages)), messages[0].timestamp if messages else None

    def check_window(self, connection: Connection, window: SummaryWindow) -> bool:
        """在提交事务内确认仍是同一批当前获准原件。"""
        messages, _ = self._window(
            window.scope,
            window.start,
            len(window.messages),
            connection=connection,
        )
        return window.messages == messages and self.allows_scope(window.scope)

    def check_evidence(
        self,
        connection: Connection,
        scope: SummaryScope,
        sequence: int,
        message_id: str,
        paths: tuple[str, ...],
    ) -> bool:
        """复验实时路由权限、原件与指定图片组件。"""
        if not self.allows_scope(scope):
            return False
        if scope == WEB_PRIVATE_SCOPE and self.timeline is not None:
            record = self.timeline.platform_history.web_private_evidence(connection, sequence)
            return record is not None and message_id == f"web:{record.id}" and not paths
        if self.store is None:
            return False
        route, visibility = scope
        message = self.store.summary_evidence(
            connection,
            ConversationKey.from_scope_key(route),
            VisibilityScope(visibility),
            sequence,
            message_id,
        )
        if message is None:
            return False
        material = _material(message)
        return not paths or (material.is_human and set(paths) <= set(material.images))
