"""网页时间线和总览：只读核心状态，复用回复上下文查询（D84）。"""

import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, HTTPException

from aureus import __version__
from aureus.common import Clock
from aureus.core import CognitiveState
from aureus.heartbeat import EVENT_REPLY_REFERENCE, recent_dialogue
from aureus.heartbeat.events import EVENT_REPLY_DELIVERED
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.reply_records import INTERNAL_REPLY_RECORD
from aureus.timeline import TimelineDatabase, TimelineQuery, TimelineRecord, VisibilityScope
from aureus.web.recall_feedback_api import feedback_available

DEFAULT_HOURS = 24
MAX_HOURS = 720
DEFAULT_LIMIT = 100
MAX_LIMIT = 300
CONVERSATION_FIELDS = ("platform", "account_id", "kind", "recipient_id")
type ConversationMetadata = dict[str, str | None]


def _conversation_key(value: str) -> ConversationKey:
    """把网页传来的会话键转成时间线 metadata 条件（平台、账号、类型、对方，D123）。"""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = None
    if not isinstance(decoded, dict):
        raise HTTPException(422, detail="会话过滤参数要是包含平台、账号、类型和对方的 JSON 对象。")
    fields: dict[str, str] = {}
    for name in CONVERSATION_FIELDS:
        item = decoded.get(name)
        if not isinstance(item, str) or not item:
            raise HTTPException(422, detail="会话过滤参数缺少平台、账号、类型或对方。")
        fields[name] = item
    temporary_group = decoded.get("temporary_group_id")
    if temporary_group is not None and (
        not isinstance(temporary_group, str) or not temporary_group
    ):
        raise HTTPException(422, detail="临时会话参数必须是非空字符串。")
    return ConversationKey(
        fields["platform"],
        fields["account_id"],
        fields["kind"],
        fields["recipient_id"],
        temporary_group,
    )


def _conversation_records(
    timeline: TimelineDatabase,
    query: TimelineQuery,
    conversation: ConversationMetadata,
) -> Sequence[TimelineRecord]:
    """只取这个会话的记录，并按轮次补上判断、规划、投递等没有会话字段的事件（D123）。"""
    fields = {key: value for key, value in conversation.items() if value is not None}
    anchors = timeline.query(query.model_copy(update={"metadata": {**query.metadata, **fields}}))
    anchors = [row for row in anchors if _matches_conversation(row, conversation)]
    if not anchors:
        return anchors
    # 轮次编号等于这一批第一条来信的记录序号；判断等事件只带 round_id，不带会话字段。
    round_ids = {record.id for record in anchors}
    for record in anchors:
        round_id = record.metadata.get("round_id")
        if isinstance(round_id, int) and not isinstance(round_id, bool) and round_id > 0:
            round_ids.add(round_id)
    related = timeline.query(query.model_copy(update={"metadata": {}, "limit": MAX_LIMIT}))
    merged = {record.id: record for record in anchors}
    for record in related:
        round_id = record.metadata.get("round_id")
        if (
            type(round_id) is int
            and round_id in round_ids
            and record.metadata.get("recipient_id") is None
        ):
            merged[record.id] = record
    records = sorted(merged.values(), key=lambda record: record.id)
    return records[-query.limit :]


def _matches_conversation(record: TimelineRecord, conversation: ConversationMetadata) -> bool:
    return all(record.metadata.get(key) == value for key, value in conversation.items())


def _conversation_delta(
    timeline: TimelineDatabase,
    records: Sequence[TimelineRecord],
    conversation: ConversationMetadata,
) -> Sequence[TimelineRecord]:
    """后续判断可能只带旧轮次编号，核对真正的开批原件，不串同号的临时会话。"""
    anchors: dict[int, TimelineRecord | None] = {}
    selected: list[TimelineRecord] = []
    for record in records:
        if _matches_conversation(record, conversation):
            selected.append(record)
            continue
        round_id = record.metadata.get("round_id")
        if record.metadata.get("recipient_id") is not None or type(round_id) is not int:
            continue
        if round_id not in anchors:
            anchors[round_id] = timeline.get(round_id)
        anchor = anchors[round_id]
        if anchor is not None and _matches_conversation(anchor, conversation):
            selected.append(record)
    return selected


def _read_timeline_page(
    timeline: TimelineDatabase,
    query: TimelineQuery,
    conversation: ConversationMetadata | None,
    recall_feedback_enabled: bool,
) -> tuple[Sequence[TimelineRecord], list[dict[str, Any]], int, bool]:
    """快照在读前取游标；之后写入的记录要么已经出现在快照里，要么能在下一页读到。"""
    if query.latest:
        cursor = timeline.get_latest_id()
        records = (
            timeline.query(query)
            if conversation is None
            else _conversation_records(timeline, query, conversation)
        )
        has_more = False
    else:
        page = timeline.query(query.model_copy(update={"limit": query.limit + 1}))
        scanned = page[: query.limit]
        cursor = scanned[-1].id if scanned else query.since_id
        has_more = len(page) > query.limit
        records = _conversation_delta(timeline, scanned, conversation) if conversation else scanned
    items = [_timeline_item(row, timeline, recall_feedback_enabled) for row in records]
    return records, items, cursor, has_more


def _timeline_item(
    record: TimelineRecord,
    timeline: TimelineDatabase,
    recall_feedback_enabled: bool,
) -> dict[str, Any]:
    metadata = {
        key: value
        for key, value in record.metadata.items()
        if key not in {"round_id", "recall_feedback_refs"}
    }
    item: dict[str, Any] = {
        "id": record.id,
        "time": record.timestamp.isoformat(),
        "actor": record.actor_type.value,
        "actor_id": record.actor_id,
        "event_type": record.event_type,
        "content": record.content,
        "visibility": record.visibility.value,
        "channel": str(metadata.get("channel", "")),
        "metadata": metadata,
    }
    round_id = record.metadata.get("round_id")
    if isinstance(round_id, int) and not isinstance(round_id, bool) and round_id > 0:
        item["round_id"] = round_id
    if not recall_feedback_enabled:
        return item
    if record.event_type == EVENT_REPLY_DELIVERED:
        reply_id = metadata.get("reply_id")
        if isinstance(reply_id, int) and isinstance(metadata.get("segment"), int):
            reply = timeline.get(reply_id)
            if (
                reply is not None
                and reply.visibility is record.visibility
                and feedback_available(timeline, reply)
            ):
                item["recall_feedback_reply_id"] = reply_id
    elif feedback_available(timeline, record):
        item["recall_feedback_available"] = True
    return item


class HeartbeatView(Protocol):
    """时间线和总览只读的心跳接口；所有调用留在事件循环线程。"""

    async def async_describe_state(self) -> str:
        """当前状态的文字说明。"""
        ...

    def context_message_limit(self) -> int:
        """当前回复上下文的条数。"""
        ...

    def working_memory_summaries(self) -> dict[str, str]:
        """已持久化的注意力滚动摘要。"""
        ...

    def working_memory_capacity(self) -> int | None:
        """当前配置启用的工作记忆容量。"""
        ...

    async def working_memory_focus(self) -> tuple[int, int | None] | None:
        """启用工作记忆时返回容量和全局焦点边界。"""
        ...

    @property
    def cognitive_state(self) -> CognitiveState | None:
        """最近一拍的认知状态。"""
        ...

    @property
    def is_thinking(self) -> bool:
        """是否有进行中的想法。"""
        ...

    def is_replying_to(
        self,
        *,
        conversation: ConversationKey | None = None,
        web_visibility: VisibilityScope | None = None,
    ) -> bool:
        """指定会话是否有进行中的回复。"""
        ...


def register_timeline_routes(
    app: FastAPI,
    *,
    timeline: TimelineDatabase,
    heartbeat: HeartbeatView,
    clock: Clock,
    visibility: VisibilityScope,
    require_ready: Callable[..., Awaitable[str]],
    restart_required: Callable[[], bool] | None = None,
    maintenance_paused: Callable[[], bool] | None = None,
    recall_feedback_enabled: bool = False,
    allows_conversation: Callable[[ConversationKey], bool] | None = None,
) -> None:
    """注册时间线和总览；核心时钟、用户可见范围和登录依赖由组装方传入。"""

    @app.get("/api/timeline")
    async def view_timeline(
        _token: Annotated[str, Depends(require_ready)],
        hours: str = str(DEFAULT_HOURS),
        before_id: str | None = None,
        since_id: str | None = None,
        limit: str = str(DEFAULT_LIMIT),
        types: str = "",
        conversation: str | None = None,
    ) -> dict[str, Any]:
        try:
            bounded_hours = min(max(int(hours), 1), MAX_HOURS)
            bounded_limit = min(max(int(limit), 1), MAX_LIMIT)
            before = None if before_id is None else int(before_id)
            after = None if since_id is None else int(since_id)
        except ValueError:
            raise HTTPException(422, detail="时间范围、条数和翻页序号要用整数。") from None
        if (after is not None and after < 0) or (before is not None and after is not None):
            raise HTTPException(422, detail="不能同时指定两个方向的游标，追新序号不能为负数。")
        since = clock.now() - timedelta(hours=bounded_hours)
        event_types = tuple({item.strip() for item in types.split(",") if item.strip()})
        query = TimelineQuery(
            since_time=since,
            before_id=before if before is not None and before > 0 else None,
            since_id=after if after is not None else 0,
            latest=after is None,
            limit=bounded_limit,
            event_types=event_types,
            without_metadata=(INTERNAL_REPLY_RECORD,),
        )
        key = _conversation_key(conversation) if conversation is not None else None
        if key is not None and (allows_conversation is None or not allows_conversation(key)):
            raise HTTPException(404, detail="会话不可用。")
        scope = (
            {
                "channel": key.platform,
                "account_id": key.account_id,
                "message_type": key.kind,
                "recipient_id": key.recipient_id,
                "temporary_group_id": key.temporary_group_id,
            }
            if key is not None
            else None
        )
        records, items, cursor, has_more = await timeline.async_read(
            _read_timeline_page, timeline, query, scope, recall_feedback_enabled
        )
        focus = await heartbeat.working_memory_focus()
        if focus is None:
            message_limit = heartbeat.context_message_limit()
            context = await timeline.async_read(
                recent_dialogue, timeline, visibility=visibility, limit=message_limit
            )
            boundary_id = context[0].id if context else None
        else:
            message_limit, boundary_id = focus
        if key is not None and (allows_conversation is None or not allows_conversation(key)):
            raise HTTPException(404, detail="会话不可用。")
        return {
            "summary": heartbeat.working_memory_summaries(),
            "records": items,
            "next_before_id": (
                records[0].id if after is None and len(records) == bounded_limit else None
            ),
            "since_id": cursor,
            "has_more": has_more,
            "context": {
                "message_limit": message_limit,
                "boundary_id": boundary_id,
                **({"working_memory": True} if focus is not None else {}),
            },
        }

    @app.get("/api/overview")
    async def overview(_token: Annotated[str, Depends(require_ready)]) -> dict[str, Any]:
        references = await timeline.async_query(
            TimelineQuery(event_type=EVENT_REPLY_REFERENCE, latest=True, limit=1)
        )
        reference = references[0] if references else None
        state = heartbeat.cognitive_state
        return {
            "version": __version__,
            "state_text": await heartbeat.async_describe_state(),
            "thinking": heartbeat.is_thinking,
            "energy_level": state.energy_level if state else None,
            "energy_tier": state.energy_state if state else "",
            "current_task": state.current_task if state else "",
            "working_memory_capacity": heartbeat.working_memory_capacity(),
            "reply_reference": (
                {
                    "content": reference.content,
                    "time": reference.timestamp.isoformat(),
                    "visibility": reference.visibility.value,
                    "channel": reference.metadata.get("channel", ""),
                }
                if reference is not None
                else None
            ),
            "restart_required": restart_required() if restart_required else False,
            **({"maintenance_paused": maintenance_paused()} if maintenance_paused else {}),
        }
