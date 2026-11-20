"""时间线数据模型。

时间线是 Aureus 的唯一经历记录：
- 只追加，不修改、不删除；更正也以新记录追加
- 每条记录有连续序号，能发现分叉
- 每条记录写明"谁做的"和"可见范围"
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ActorType(StrEnum):
    """时间线记录的执行者类型。"""

    ME = "me"  # Aureus 自己
    USER = "user"  # 用户
    PERSON = "person"  # 某个联系人（使用人物 ID）
    SYSTEM = "system"  # 外部系统


class VisibilityScope(StrEnum):
    """时间线记录的可见范围。"""

    PRIVATE = "private"  # 仅自己可见
    USER_ONLY = "user_only"  # 用户私聊（仅用户可见）
    GROUP = "group"  # 群聊或群组成员可见
    PUBLIC = "public"  # 所有人可见


class TimelineRecord(BaseModel):
    """时间线上的一条记录（内存表示）。

    注：数据库中的实际字段见 TimelineDatabase。
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="全局唯一序号（自动递增，禁止修改和删除）")
    timestamp: datetime = Field(..., description="事件发生时间（UTC）")
    actor_type: ActorType = Field(..., description="执行者类型")
    actor_id: str = Field(
        default="", description="执行者 ID（user_id 或 person_id，actor_type=me 时留空）"
    )
    event_type: str = Field(..., description="事件类型（message、action、state_change 等）")
    content: str = Field(default="", description="事件内容")
    visibility: VisibilityScope = Field(..., description="可见范围")
    metadata: dict = Field(default_factory=dict, description="额外信息（key-value 对）")

    def summary(self) -> str:
        """生成此记录的一行摘要。"""
        actor_desc = {
            ActorType.ME: "我",
            ActorType.USER: "用户",
            ActorType.PERSON: f"人物:{self.actor_id}",
            ActorType.SYSTEM: "系统",
        }.get(self.actor_type, self.actor_type)

        ts = self.timestamp.isoformat()
        content_preview = self.content[:50]
        return f"[{self.id}] {ts} - {actor_desc} - {self.event_type}: {content_preview}"


class TimelineQuery(BaseModel):
    """时间线查询条件。"""

    model_config = ConfigDict(extra="forbid")

    since_id: int = Field(default=0, description="从哪条记录 ID 之后开始查询")
    limit: int = Field(default=100, description="最多返回多少条记录")
    event_type: str = Field(default="", description="按事件类型过滤（留空表示不过滤）")
    actor_type: ActorType | None = Field(default=None, description="按执行者类型过滤")
    visible_to_current_user: bool = Field(default=False, description="只返回当前用户可见的记录")
    visibility: VisibilityScope | None = Field(default=None, description="只返回这个可见范围的记录")
    latest: bool = Field(
        default=False, description="只取符合条件的最新 limit 条（结果仍按序号升序）"
    )
    metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, description="metadata 的顶层标量精确匹配，所有条件都要满足"
    )
