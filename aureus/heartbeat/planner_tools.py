"""会话规划可用的工具目录及有界执行（D103）。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from aureus.common.limits import MAX_INTEREST_CHARS
from aureus.heartbeat.planning import Plan, parse_plan_data
from aureus.organs import OrganRequest, OrganResponse, ToolDefinition, ToolExchange, ToolResult

_MAX_ROUNDS = 2


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class _ReplyArguments(_Arguments):
    addressed_to_me: Literal["yes", "no", "uncertain"] = Field(
        description="先判断是否在对你说话：yes 是，no 否，uncertain 不确定。"
    )
    reply_reference: str = Field(
        min_length=1, max_length=300, description="回复所需的上下文关键信息与回复方向。"
    )
    intent: str | None = Field(default=None, min_length=1, max_length=300, description="简短回复方向。")
    reason: str | None = Field(default=None, max_length=120, description="一句可展示的行动依据，不写推理过程。")
    length: Literal["短", "中", "长"] | None = Field(default=None, description="建议篇幅。")


class _Commit(_Arguments):
    what: str = Field(min_length=1, max_length=200)
    due: str | None = Field(default=None, max_length=100)


class _Resolution(_Arguments):
    id: int = Field(ge=1)
    result: Literal["done", "dropped"]
    reason: str | None = Field(default=None, max_length=200)


class _Interest(_Arguments):
    action: Literal["add", "remove", "replace"]
    text: str | None = Field(default=None, min_length=1, max_length=MAX_INTEREST_CHARS)
    interest_id: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=100)


_ExpressionText = Annotated[str, Field(min_length=1, max_length=80)]
_ExpressionTerms = _ExpressionText | Annotated[list[_ExpressionText], Field(max_length=3)]


class _Expression(_Arguments):
    focus: _ExpressionTerms | None = None
    reply_act: _ExpressionTerms | None = None
    scene: _ExpressionTerms | None = None
    tone: _ExpressionTerms | None = None
    prefer: _ExpressionTerms | None = None
    avoid: _ExpressionTerms | None = None


class _Emoji(_Arguments):
    emotion: str = Field(default="", max_length=300)
    before_text: bool = False


class _QueryArguments(_Arguments):
    keywords: list[Annotated[str, Field(min_length=2, max_length=20)]] = Field(
        min_length=1, max_length=3, description="与当前聊天相关的关键词，最多三个。"
    )


@dataclass(frozen=True)
class PlannerToolOptions:
    """按当前会话权限和已接入能力决定声明哪些工具或参数。"""

    recall: bool = False
    agenda: bool = False
    interests: bool = False
    expressions: bool = False
    emoji: bool = False
    inbox: bool = False
    special_contact: bool = False
    stay_in_conversation: bool = False


def _reply(arguments: _Arguments) -> Plan | None:
    data = arguments.model_dump(exclude_none=True)
    data.setdefault("intent", data["reply_reference"])
    plan = parse_plan_data({"reply": True, **data})
    if plan is None or ("interest" in data and plan.interest is None):
        return None
    if len(data.get("resolve", ())) != len(plan.resolutions):
        return None
    return replace(plan, addressed_to_me=data["addressed_to_me"])


def _stop(arguments: _Arguments) -> Plan:
    return Plan(False, "", None, "")


def _inbox(arguments: _Arguments) -> Plan | None:
    data = arguments.model_dump(exclude_none=True)
    return parse_plan_data({"reply": False, **data}) if data else None


def _query(arguments: _Arguments) -> tuple[str, ...]:
    assert isinstance(arguments, _QueryArguments)
    return tuple(dict.fromkeys(arguments.keywords))


@dataclass(frozen=True)
class PlannerTool:
    """一个工具的说明、参数模型和执行入口；发出的定义从这里生成。"""

    name: str
    description: str
    arguments: type[_Arguments]
    handle: Callable[[_Arguments], Plan | tuple[str, ...] | None]

    def definition(self) -> ToolDefinition:
        """供模型或管理端使用的同源参数声明。"""
        return ToolDefinition(self.name, self.description, self.arguments.model_json_schema())


def planner_tools(options: PlannerToolOptions = PlannerToolOptions()) -> tuple[PlannerTool, ...]:
    """返回当前能力下唯一对外的工具目录。"""
    inbox: dict[str, Any] = {}
    if options.inbox:
        inbox["read_inbox"] = (
            Literal[True] | None, Field(default=None, description="空闲时翻阅平台收件箱。")
        )
        if options.special_contact:
            inbox["special_contact"] = (
                bool | None, Field(default=None, description="仅对当前已认识、非主人的联系人设置或取消特别关心。")
            )
        if options.stay_in_conversation:
            inbox["stay_in_conversation"] = (
                bool | None, Field(default=None, description="当前消息来自平台收件箱时，留在本会话持续聊天，或退出。")
            )
    extras = dict(inbox)
    if options.agenda:
        extras.update(
            commit=(_Commit | None, Field(default=None, description="本次答应的事；日期用当地时间，未约定留空。")),
            resolve=(list[_Resolution] | None, Field(default=None, max_length=5, description="已完成或无法完成的日程；放弃须简述原因。")),
        )
    if options.interests:
        extras["interest"] = (_Interest | None, Field(default=None, description="仅已确认用户的私聊可提出兴趣建议；add 填文字，remove 填编号，replace 两者都填。"))
    if options.expressions:
        extras["expression_intent"] = (_Expression | None, Field(default=None, description="可供选择习得表达的情景、语气等，只填相关项。"))
    if options.emoji:
        extras["select_emoji"] = (_Emoji | None, Field(default=None, description="仅 QQ 会话可建议按情绪匹配表情包，选择失败时仅发文字。"))
    tools = [
        PlannerTool(
            "reply", "仅在应该对当前会话发出可见回复时使用；提交回复方向，不写最终发言。",
            create_model("ReplyArguments", __base__=_ReplyArguments, **extras), _reply,
        ),
        PlannerTool("stop", "结束本轮规划并等待新消息，不发言也不设置定时等待。", _Arguments, _stop),
    ]
    if options.recall:
        tools.append(PlannerTool(
            "query_memory", "按相关关键词读取获准访问的记忆，用工具结果继续规划。",
            _QueryArguments, _query,
        ))
    if inbox:
        tools.append(PlannerTool(
            "inbox_attention", "只提交本会话的收件箱关注建议，结束规划且不发言。",
            create_model("InboxArguments", __base__=_Arguments, **inbox), _inbox,
        ))
    return tuple(tools)


async def dispatch_planner_tools[Recall](
    request: OrganRequest,
    tools: tuple[PlannerTool, ...],
    call_organ: Callable[[OrganRequest], Awaitable[OrganResponse]],
    retrieve: Callable[[tuple[str, ...]], Awaitable[tuple[str, Recall] | None]],
) -> tuple[Plan | None, Recall | None]:
    """只接受已声明的合法调用。最多读取一次，工具输出通过同一请求的历史传回。"""
    by_name = {tool.name: tool for tool in tools}
    request = replace(request, tools=tuple(tool.definition() for tool in tools))
    recalled: Recall | None = None
    for round_number in range(_MAX_ROUNDS):
        response = await call_organ(request)
        if response.metadata.get("tool_calls_invalid") or len(response.tool_calls) != 1:
            break
        call = response.tool_calls[0]
        tool = by_name.get(call.name)
        if tool is None or not call.call_id or (
            request.tool_history and call.call_id == request.tool_history[-1].calls[0].call_id
        ):
            break
        try:
            arguments = tool.arguments.model_validate(call.arguments)
        except ValidationError:
            break
        result = tool.handle(arguments)
        if isinstance(result, Plan):
            return result, recalled
        if result is None or round_number + 1 == _MAX_ROUNDS:
            break
        fetched = await retrieve(result)
        if fetched is None:
            break
        content, recalled = fetched
        exchange = ToolExchange((call,), (ToolResult(call.call_id, content),))
        request = replace(request, tool_history=(*request.tool_history, exchange))
    return None, None
