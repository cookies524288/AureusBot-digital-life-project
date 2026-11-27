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


def _conversation_metadata(value: str) -> dict[str, str]:
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
    return {
        "channel": fields["platform"],
        "account_id": fields["account_id"],
        "message_type": fields["kind"],
        "recipient_id": fields["recipient_id"],
    }


def _conversation_records(
    timeline: TimelineDatabase,
    query: TimelineQuery,
    conversation: dict[str, str],
) -> Sequence[TimelineRecord]:
    """只取这个会话的记录，并按轮次补上判断、规划、投递等没有会话字段的事件（D123）。"""
    anchors = timeline.query(
        query.model_copy(update={"metadata": {**query.metadata, **conversation}})
    )
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
        if isinstance(round_id, int) and not isinstance(round_id, bool) and round_id in round_ids:
            merged[record.id] = record
    records = sorted(merged.values(), key=lambda record: record.id)
    return records[-query.limit :]


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


def _read_timeline_page(
    timeline: TimelineDatabase,
    query: TimelineQuery,
    conversation: dict[str, str] | None,
    recall_feedback_enabled: bool,
) -> tuple[Sequence[TimelineRecord], list[dict[str, Any]]]:
    records = (
        timeline.query(query)
        if conversation is None
        else _conversation_records(timeline, query, conversation)
    )
    return records, [_timeline_item(row, timeline, recall_feedback_enabled) for row in records]


class HeartbeatView(Protocol):
    """时间线和总览只读的心跳接口；所有调用留在事件循环线程。"""

    def describe_state(self) -> str:
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

    def working_memory_focus(self) -> tuple[int, int | None] | None:
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
) -> None:
    """注册时间线和总览；核心时钟、用户可见范围和登录依赖由组装方传入。"""

    @app.get("/api/timeline")
    async def view_timeline(
        _token: Annotated[str, Depends(require_ready)],
        hours: str = str(DEFAULT_HOURS),
        before_id: str | None = None,
        limit: str = str(DEFAULT_LIMIT),
        types: str = "",
        conversation: str | None = None,
    ) -> dict[str, Any]:
        try:
            bounded_hours = min(max(int(hours), 1), MAX_HOURS)
            bounded_limit = min(max(int(limit), 1), MAX_LIMIT)
            before = None if before_id is None else int(before_id)
        except ValueError:
            raise HTTPException(422, detail="时间范围、条数和翻页序号要用整数。") from None
        since = clock.now() - timedelta(hours=bounded_hours)
        event_types = tuple({item.strip() for item in types.split(",") if item.strip()})
        query = TimelineQuery(
            since_time=since,
            before_id=before if before is not None and before > 0 else None,
            latest=True,
            limit=bounded_limit,
            event_types=event_types,
            without_metadata=(INTERNAL_REPLY_RECORD,),
        )
        records, items = await timeline.async_read(
            _read_timeline_page,
            timeline,
            query,
            _conversation_metadata(conversation) if conversation is not None else None,
            recall_feedback_enabled,
        )
        focus = heartbeat.working_memory_focus()
        if focus is None:
            message_limit = heartbeat.context_message_limit()
            context = await timeline.async_read(
                recent_dialogue, timeline, visibility=visibility, limit=message_limit
            )
            boundary_id = context[0].id if context else None
        else:
            message_limit, boundary_id = focus
        return {
            "summary": heartbeat.working_memory_summaries(),
            "records": items,
            "next_before_id": records[0].id if len(records) == bounded_limit else None,
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
            "state_text": heartbeat.describe_state(),
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
