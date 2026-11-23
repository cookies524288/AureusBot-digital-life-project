"""网页时间线和总览：只读核心状态，复用回复上下文查询（D84）。"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, HTTPException

from aureus import __version__
from aureus.common import Clock
from aureus.core import CognitiveState
from aureus.heartbeat import EVENT_REPLY_REFERENCE, recent_dialogue
from aureus.timeline import TimelineDatabase, TimelineQuery, VisibilityScope
from aureus.web.recall_feedback_api import feedback_available

DEFAULT_HOURS = 24
MAX_HOURS = 720
DEFAULT_LIMIT = 100
MAX_LIMIT = 300


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
    ) -> dict[str, Any]:
        try:
            bounded_hours = min(max(int(hours), 1), MAX_HOURS)
            bounded_limit = min(max(int(limit), 1), MAX_LIMIT)
            before = None if before_id is None else int(before_id)
        except ValueError:
            raise HTTPException(422, detail="时间范围、条数和翻页序号要用整数。") from None
        since = clock.now() - timedelta(hours=bounded_hours)
        event_types = {item.strip() for item in types.split(",") if item.strip()} or {""}
        records = [
            record
            for event_type in event_types
            for record in timeline.query(
                TimelineQuery(
                    since_time=since,
                    before_id=before if before is not None and before > 0 else None,
                    latest=True,
                    limit=bounded_limit,
                    event_type=event_type,
                )
            )
        ]
        records = sorted(records, key=lambda record: record.id)[-bounded_limit:]
        focus = heartbeat.working_memory_focus()
        if focus is None:
            message_limit = heartbeat.context_message_limit()
            context = recent_dialogue(timeline, visibility=visibility, limit=message_limit)
            boundary_id = context[0].id if context else None
        else:
            message_limit, boundary_id = focus
        return {
            "summary": heartbeat.working_memory_summaries(),
            "records": [
                {
                    "id": record.id,
                    "time": record.timestamp.isoformat(),
                    "actor": record.actor_type.value,
                    "actor_id": record.actor_id,
                    "event_type": record.event_type,
                    "content": record.content,
                    "visibility": record.visibility.value,
                    "channel": str(record.metadata.get("channel", "")),
                    "metadata": {
                        key: value
                        for key, value in record.metadata.items()
                        if key != "recall_feedback_refs"
                    },
                    **(
                        {"recall_feedback_available": True}
                        if recall_feedback_enabled and feedback_available(timeline, record)
                        else {}
                    ),
                }
                for record in records
            ],
            "next_before_id": records[0].id if len(records) == bounded_limit else None,
            "context": {
                "message_limit": message_limit,
                "boundary_id": boundary_id,
                **({"working_memory": True} if focus is not None else {}),
            },
        }

    @app.get("/api/overview")
    async def overview(_token: Annotated[str, Depends(require_ready)]) -> dict[str, Any]:
        state = heartbeat.cognitive_state
        references = timeline.query(
            TimelineQuery(event_type=EVENT_REPLY_REFERENCE, latest=True, limit=1)
        )
        reference = references[0] if references else None
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
