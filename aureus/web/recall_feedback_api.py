"""维护者只对已实际投递的回复所用的召回依据追加评价。"""

from collections.abc import Awaitable, Callable, Iterator
from hashlib import sha256
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from aureus.heartbeat.events import (
    EVENT_MESSAGE,
    EVENT_RECALL_FEEDBACK,
    EVENT_RECALL_REPLY_DELIVERED,
)
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

type Rating = Literal["useful", "not_useful"]


class RecallFeedbackUpdate(BaseModel):
    """只接收已绑定到该回复的证据键和人工评价；空值表示撤销。"""

    model_config = ConfigDict(extra="forbid")
    evidence_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    rating: Rating | None


def feedback_refs(reply: TimelineRecord) -> Iterator[tuple[str, str]]:
    """映射经精确留痕的依据；旧版召回元数据不用于推测。"""
    if (
        reply.event_type != EVENT_MESSAGE
        or reply.actor_type is not ActorType.ME
        or reply.visibility is VisibilityScope.PRIVATE
    ):
        return
    for reference in dict.fromkeys(reply.metadata.get("recall_feedback_refs", ())):
        kind, _, identity = reference.partition(":")
        evidence_key = sha256(reference.encode()).hexdigest()
        if kind == "timeline":
            yield evidence_key, f"时间线记录 #{identity}"
        elif kind == "source":
            digest = sha256(identity.encode()).hexdigest()[:12]
            yield evidence_key, f"记忆依据 · 编号 {digest}"


def _rating(timeline: TimelineDatabase, reply: TimelineRecord, key: str) -> Rating | None:
    events = timeline.query(
        TimelineQuery(
            event_type=EVENT_RECALL_FEEDBACK,
            actor_type=ActorType.USER,
            visibility=reply.visibility,
            metadata={"reply_id": reply.id, "evidence_key": key},
            latest=True,
            limit=1,
        )
    )
    return events[0].metadata["rating"] if events else None


def feedback_available(timeline: TimelineDatabase, reply: TimelineRecord) -> bool:
    """至少一段文字实际投递成功后，才允许评价整轮使用的依据。"""
    return next(feedback_refs(reply), None) is not None and bool(
        timeline.query(
            TimelineQuery(
                event_type=EVENT_RECALL_REPLY_DELIVERED,
                actor_type=ActorType.SYSTEM,
                visibility=reply.visibility,
                metadata={"reply_id": reply.id},
                limit=1,
            )
        )
    )


def register_recall_feedback_routes(
    app: FastAPI,
    timeline: TimelineDatabase,
    require_ready: Callable[..., Awaitable[str]],
    same_origin: Callable[[Request], bool],
    *,
    enabled: bool,
) -> None:
    """查询与写入共用投递、证据绑定及可见性校验。"""

    def eligible(reply_id: int) -> tuple[TimelineRecord, dict[str, str]]:
        if not enabled:
            raise HTTPException(404, "召回评价已关闭。")
        reply = timeline.get(reply_id)
        if reply is not None and feedback_available(timeline, reply):
            return reply, dict(feedback_refs(reply))
        raise HTTPException(404, "没有可评价的召回依据。")

    @app.get("/api/timeline/{reply_id}/recall-feedback")
    async def read_feedback(
        reply_id: int, _token: Annotated[str, Depends(require_ready)]
    ) -> list[dict[str, str | None]]:
        reply, references = eligible(reply_id)
        return [
            {"key": key, "label": label, "rating": _rating(timeline, reply, key)}
            for key, label in references.items()
        ]

    @app.patch("/api/timeline/{reply_id}/recall-feedback")
    async def write_feedback(
        request: Request,
        reply_id: int,
        body: RecallFeedbackUpdate,
        _token: Annotated[str, Depends(require_ready)],
    ) -> dict[str, Rating | None]:
        if not same_origin(request):
            raise HTTPException(403, "请在当前网页上评价召回依据。")
        reply, references = eligible(reply_id)
        key = body.evidence_key
        if key not in references:
            raise HTTPException(404, "该依据不属于这条回复。")
        if _rating(timeline, reply, key) != body.rating:
            description = {"useful": "标为有用", "not_useful": "标为无用", None: "撤销评价"}
            timeline.append(
                EVENT_RECALL_FEEDBACK,
                "召回依据：" + description[body.rating] + "。",
                ActorType.USER,
                visibility=reply.visibility,
                metadata={"reply_id": reply.id, "evidence_key": key, "rating": body.rating},
            )
        return {"rating": body.rating}
