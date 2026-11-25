"""工具规划必须有真实、可校验的发言选择，不采用普通输出。"""

import pytest

from aureus.heartbeat.planner_tools import (
    PlannerAction,
    PlannerToolOptions,
    dispatch_planner_tools,
    planner_tools,
)
from aureus.organs import OrganRequest, OrganResponse, ToolCall

_REPLY = ToolCall(
    "reply", "reply-1", {"addressed_to_me": "no", "reply_reference": "只补充相关信息"}
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "calls, invalid",
    [
        ((), False),
        ((_REPLY,), True),
        ((ToolCall("reply", "malformed", {"addressed_to_me": "yes"}),), False),
        (
            (ToolCall("reply", "bad-type", {"addressed_to_me": True, "reply_reference": "回应"}),),
            False,
        ),
        ((ToolCall("stop", "wrong-stop", {"unexpected": True}),), False),
        ((_REPLY, ToolCall("stop", "stop-1", {})), False),
        ((ToolCall("query_memory", "undeclared", {"keywords": ["木卫二"]}),), False),
    ],
)
async def test_missing_or_invalid_tool_cannot_authorize_reply(
    calls: tuple[ToolCall, ...], invalid: bool
) -> None:
    async def call(request: OrganRequest) -> OrganResponse:
        return OrganResponse(
            request.organ_id,
            '{"reply":true,"intent":"不要采用普通输出"}',
            metadata={"tool_calls_invalid": invalid},
            tool_calls=calls,
        )

    async def retrieve(words: tuple[str, ...], limit: int | None) -> tuple[str, str] | None:
        pytest.fail(f"不应读取记忆：{words}，limit={limit}")

    actions: list[PlannerAction] = []
    assert await dispatch_planner_tools(
        OrganRequest("planner", "当前消息"),
        planner_tools(),
        call,
        retrieve,
        actions=actions,
    ) == (None, None)
    assert actions[-1].status == ("rejected" if calls or invalid else "not_run")
    assert all(not action.reference and action.status != "completed" for action in actions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "available, value, accepted",
    [
        (False, True, False),
        (True, True, True),
        (True, False, True),
        (True, "701", False),
    ],
)
async def test_reply_quote_requires_capability_and_boolean(
    available: bool,
    value: bool | str,
    accepted: bool,
) -> None:
    tools = planner_tools(PlannerToolOptions(quote_source=available))
    reply_tool = next(tool for tool in tools if tool.name == "reply")
    properties = reply_tool.arguments.model_json_schema()["properties"]
    assert ("quote_source" in properties) is available
    if available:
        assert properties["quote_source"]["default"] is False

    async def call(request: OrganRequest) -> OrganResponse:
        reply = ToolCall("reply", "quote-1", {**_REPLY.arguments, "quote_source": value})
        return OrganResponse(request.organ_id, "", tool_calls=(reply,))

    async def retrieve(words: tuple[str, ...], limit: int | None) -> tuple[str, str] | None:
        pytest.fail(f"不应读取记忆：{words}，limit={limit}")

    plan, _ = await dispatch_planner_tools(
        OrganRequest("planner", "当前消息"), tools, call, retrieve
    )
    if accepted:
        assert plan is not None and plan.quote_source is value
    else:
        assert plan is None


@pytest.mark.asyncio
async def test_invalid_non_interest_argument_still_rejects_whole_call() -> None:
    """兴趣以外的字段不合格时整条拒绝，不因为丢弃兴趣而放行（D103、D115）。"""

    async def call(request: OrganRequest) -> OrganResponse:
        reply = ToolCall(
            "reply",
            "bad-addressed",
            {
                "addressed_to_me": "maybe",
                "reply_reference": "回应",
                "interest": {"action": "add", "text": "天文", "reason": "想看看"},
            },
        )
        return OrganResponse(request.organ_id, "", tool_calls=(reply,))

    async def retrieve(words: tuple[str, ...], limit: int | None) -> tuple[str, str] | None:
        pytest.fail(f"不应读取记忆：{words}，limit={limit}")

    actions: list[PlannerAction] = []
    assert await dispatch_planner_tools(
        OrganRequest("planner", "当前消息"),
        planner_tools(PlannerToolOptions(interests=True)),
        call,
        retrieve,
        actions=actions,
    ) == (None, None)
    assert actions[-1].status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [_REPLY, ToolCall("stop", "stop-1", {})])
async def test_only_explicit_terminal_choice_after_real_tool_result(finish: ToolCall) -> None:
    requests: list[OrganRequest] = []
    query = ToolCall("query_memory", "query-1", {"keywords": ["木卫二"]})

    async def call(request: OrganRequest) -> OrganResponse:
        requests.append(request)
        return OrganResponse(
            request.organ_id, "", tool_calls=(finish if request.tool_history else query,)
        )

    async def retrieve(words: tuple[str, ...], limit: int | None) -> tuple[str, tuple[str, ...]]:
        assert words == ("木卫二",) and limit is None
        return "限量内部记忆", words

    actions: list[PlannerAction] = []
    plan, recalled = await dispatch_planner_tools(
        OrganRequest("planner", "当前消息"),
        planner_tools(PlannerToolOptions(recall=True)),
        call,
        retrieve,
        actions=actions,
    )
    first, second = requests
    assert first.prompt == second.prompt
    [exchange] = second.tool_history
    assert exchange.calls == (query,)
    assert exchange.results[0].call_id == query.call_id
    assert exchange.results[0].content == "限量内部记忆"
    assert recalled == ("木卫二",)
    assert plan is not None and plan.should_reply is (finish.name == "reply")
    assert plan.addressed_to_me == ("no" if finish.name == "reply" else None)
    assert [(action.action, action.status) for action in actions] == [
        ("query_memory", "requested"),
        ("query_memory", "completed"),
        (finish.name, "requested"),
        (finish.name, "completed"),
    ]
    assert actions[-1].reference == ("只补充相关信息" if finish.name == "reply" else "")
    assert "限量内部记忆" not in str(actions)
    assert "木卫二" not in str(actions)
