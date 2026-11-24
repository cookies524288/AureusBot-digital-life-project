"""器官层：沿用 LiteLLM 的对话工具格式，读出 D103 的调用建议。"""

import json
from dataclasses import dataclass, field
from typing import Any

from aureus.organs.interface import ToolCall, ToolDefinition, ToolExchange


def encode_tools(tools: tuple[ToolDefinition, ...]) -> list[dict[str, Any]]:
    """把声明的函数工具转成 LiteLLM 使用的参数格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def encode_tool_history(history: tuple[ToolExchange, ...]) -> list[dict[str, Any]]:
    """每批助手调用后紧接同批工具结果，保留模型提供的调用 ID。"""
    messages: list[dict[str, Any]] = []
    for exchange in history:
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }
                    for call in exchange.calls
                ],
            }
        )
        messages.extend(
            {"role": "tool", "tool_call_id": result.call_id, "content": result.content}
            for result in exchange.results
        )
    return messages


def _field(value: object, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


@dataclass
class _CallParts:
    call_id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)

    def append(self, fragment: object) -> bool:
        call_id = _field(fragment, "id")
        function = _field(fragment, "function")
        name = _field(function, "name")
        arguments = _field(function, "arguments")
        if (
            _field(fragment, "type") not in (None, "function")
            or any(
                value is not None and not isinstance(value, str)
                for value in (call_id, name, arguments)
            )
            or (self.call_id and call_id and self.call_id != call_id)
            or (self.name and name and self.name != name)
        ):
            return False
        self.call_id = call_id or self.call_id
        self.name = name or self.name
        if arguments:
            self.arguments.append(arguments)
        return True

    def parse(self) -> ToolCall | None:
        if not self.call_id.strip() or not self.name:
            return None
        try:
            arguments = json.loads("".join(self.arguments))
            json.dumps(arguments, allow_nan=False)
        except (ValueError, RecursionError):
            return None
        if not isinstance(arguments, dict):
            return None
        return ToolCall(self.name, self.call_id, arguments)


class ToolCallCollector:
    """收齐一个模型响应的工具调用；流式参数按工具索引拼接后再解码。"""

    def __init__(self, names: set[str]) -> None:
        self._names = names
        self._parts: dict[int, _CallParts] = {}
        self._invalid = False
        self._finish_reason: str | None = None

    def read_choice(self, choice: object, *, stream: bool) -> None:
        """读取普通消息或流式分片；没有工具的分片不影响已收集的调用。"""
        self._finish_reason = _field(choice, "finish_reason") or self._finish_reason
        calls = _field(_field(choice, "delta" if stream else "message"), "tool_calls")
        if calls is None:
            return
        if not isinstance(calls, (list, tuple)):
            self._invalid = True
            return
        for position, fragment in enumerate(calls):
            index = _field(fragment, "index") if stream else position
            if (
                type(index) is not int
                or index < 0
                or not self._parts.setdefault(index, _CallParts()).append(fragment)
            ):
                self._invalid = True

    def finish(self) -> tuple[ToolCall, ...] | None:
        """返回完整且已声明的调用；有任何不可信的调用时整批返回 None。"""
        if self._invalid or (self._finish_reason == "tool_calls" and not self._parts):
            return None
        if self._parts and self._finish_reason in ("length", "content_filter"):
            return None
        calls: list[ToolCall] = []
        seen: set[str] = set()
        for index in sorted(self._parts):
            call = self._parts[index].parse()
            if call is None or call.name not in self._names or call.call_id in seen:
                return None
            seen.add(call.call_id)
            calls.append(call)
        return tuple(calls)
