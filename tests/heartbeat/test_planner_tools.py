"""工具规划必须有真实、可校验的发言选择，不采用普通输出。"""

import pytest

from aureus.heartbeat.planner_tools import (
    PlannerAction,
    PlannerToolOptions,
    dispatch_planner_tools,
    planner_tools,
)
from aureus.organs import OrganRequest, OrganResponse, ToolCall

_REPLY = ToolCall("reply", "reply-1", {"addressed_to_me": "no", "reply_reference": "只补充相关信息"})


@pytest.mark.asyncio
@pytest.mark.parametrize("calls, invalid", [
    ((), False),
    ((_REPLY,), True),
    ((ToolCall("reply", "malformed", {"addressed_to_me": "yes"}),), False),
    ((ToolCall("reply", "bad-type", {"addressed_to_me": True, "reply_reference": "回应"}),), False),
    ((ToolCall("stop", "wrong-stop", {"unexpected": True}),), False),
    ((_REPLY, ToolCall("stop", "stop-1", {})), False),
    ((ToolCall("query_memory", "undeclared", {"keywords": ["木卫二"]}),), False),
])
async def test_missing_or_invalid_tool_cannot_authorize_reply(
    calls: tuple[ToolCall, ...], invalid: bool
) -> None:
    async def call(request: OrganRequest) -> OrganResponse:
        return OrganResponse(
            request.organ_id, '{"reply":true,"intent":"不要采用普通输出"}',
            metadata={"tool_calls_invalid": invalid}, tool_calls=calls,
        )

    async def retrieve(words: tuple[str, ...]) -> tuple[str, str] | None:
        pytest.fail(f"不应读取记忆：{words}")

    actions: list[PlannerAction] = []
    assert await dispatch_planner_tools(
        OrganRequest("planner", "当前消息"), planner_tools(), call, retrieve,
        actions=actions,
    ) == (None, None)
    assert actions[-1].status == ("rejected" if calls or invalid else "not_run")
    assert all(not action.reference and action.status != "completed" for action in actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [_REPLY, ToolCall("stop", "stop-1", {})])
async def test_only_explicit_terminal_choice_after_real_tool_result(finish: ToolCall) -> None:
    requests: list[OrganRequest] = []
    query = ToolCall("query_memory", "query-1", {"keywords": ["木卫二"]})

    async def call(request: OrganRequest) -> OrganResponse:
        requests.append(request)
        return OrganResponse(request.organ_id, "", tool_calls=(finish if request.tool_history else query,))

    async def retrieve(words: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        assert words == ("木卫二",)
        return "限量内部记忆", words

    actions: list[PlannerAction] = []
    plan, recalled = await dispatch_planner_tools(
        OrganRequest("planner", "当前消息"), planner_tools(PlannerToolOptions(recall=True)), call, retrieve,
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
        ("query_memory", "requested"), ("query_memory", "completed"),
        (finish.name, "requested"), (finish.name, "completed"),
    ]
    assert actions[-1].reference == ("只补充相关信息" if finish.name == "reply" else "")
    assert "限量内部记忆" not in str(actions)
    assert "木卫二" not in str(actions)
