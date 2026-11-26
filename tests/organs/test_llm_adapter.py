"""LLMOrgan（真实器官）的测试：全部用假的调用函数，不联网、不花钱。"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
from loguru import logger

from aureus.common import ManualClock
from aureus.common.config import ConfigError
from aureus.organs import llm_adapter
from aureus.organs.api_keys import KeyResolver
from aureus.organs.interface import (
    CallAttempt,
    OrganCallCancelled,
    OrganCallError,
    OrganRequest,
    ToolCall,
    ToolDefinition,
    ToolExchange,
    ToolResult,
)
from aureus.organs.llm_adapter import (
    _MODEL_TEST_IMAGE_BASE64,
    _MODEL_TEST_INPUT,
    _MODEL_TEST_TOOL_NAME,
    ASSUMED_COMPLETION_TOKENS,
    EMPTY_REPLY_REASON,
    EMPTY_SYSTEM_MESSAGE,
    LITELLM_PREFIX_BY_PROTOCOL,
    MODEL_TEST_TOOL_MISSING_REASON,
    REASONING_ONLY_REASON,
    RETRY_BACKOFF_SECONDS,
    EmbeddingOrgan,
    LLMOrgan,
    ModelProbeError,
    _model_test_prompt,
    _model_test_tool,
    close_shared_clients,
    count_prompt_tokens,
    model_probe_inputs,
    probe_model,
)
from aureus.organs.model_config import (
    ModelConfig,
    ModelRoute,
    ModelsConfig,
    ProviderConfig,
    ReasoningLevel,
)


@pytest.mark.asyncio
async def test_image_embedding_uses_jina_protocol_and_reports_usage(tmp_path: Path) -> None:
    provider = ProviderConfig(name="jina", protocol="openai")
    model = ModelConfig(
        name="picture", identifier="jina-embeddings-v4", provider="jina", kind="image_embedding"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.jina.ai/v1/embeddings"
        assert b'"image":"YWJj"' in request.content
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [3, 4]}], "usage": {"total_tokens": 7}},
        )

    organ = EmbeddingOrgan(
        "image_embedding",
        [ModelRoute(model, provider, None, None)],
        KeyResolver(tmp_path / "test.env"),
        transport=httpx.MockTransport(respond),
    )
    response = await organ.process(OrganRequest("image_embedding", "", embedding_inputs=(b"abc",)))
    assert response.embeddings == ((3.0, 4.0),)
    assert response.attempts == [CallAttempt("picture", "jina", 7, 0)]


CLOUD_SECRET = "sk-cloud-secret"


class RateLimitError(Exception):
    """和 litellm 的同名异常一样，适配器只看类名。"""


# 用 type() 创建：类名必须叫 Timeout（和 litellm 一致），但不以 Error 结尾，
# 直接写 class 语句会被命名规则（N818）拦下。
Timeout = type("Timeout", (Exception,), {})


class FakeCompletion:
    """假的调用函数：按顺序给出预设的结果或异常，并记下每次收到的参数。"""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SleepRecorder:
    """假的等待函数：不真的等，只记下每次想等多久。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_response(
    text: str | None, prompt: int = 100, completion: int = 20, cached: int = 0
) -> Any:
    message = SimpleNamespace(content=text)
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


@pytest.mark.asyncio
async def test_media_organ_routes_and_requests(tmp_path: Path) -> None:
    provider = ProviderConfig(name="test", protocol="openai_responses")
    chat = ModelConfig(name="chat", identifier="chat", provider="test")
    vision = ModelConfig(name="image", identifier="image", provider="test", vision=True)
    voice = ModelConfig(name="voice", identifier="voice", provider="test", kind="transcription")
    routes = [ModelRoute(model, provider, None, None) for model in (chat, vision, voice)]
    key_resolver = KeyResolver(tmp_path / "test.env")
    visual_model = FakeCompletion(make_response("图片说明"))
    vlm = LLMOrgan("vlm", routes, key_resolver, completion=visual_model)
    response = await vlm.process(
        OrganRequest("vlm", "描述图片", media_bytes=b"abc", media_type="image/png")
    )
    assert response.suggestion == "图片说明"
    assert visual_model.calls[0]["model"] == "openai/responses/image"
    assert visual_model.calls[0]["messages"][-1]["content"][-1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,YWJj"},
    }
    speech_model = FakeCompletion(SimpleNamespace(text=" 转写文本 "))
    speech = LLMOrgan("voice", routes, key_resolver, completion=speech_model)
    response = await speech.process(
        OrganRequest("voice", "", media_bytes=b"ID3voice", media_type="audio/mpeg")
    )
    assert response.suggestion == "转写文本"
    assert len(response.attempts) == 1
    assert speech_model.calls[0]["model"] == "openai/voice"
    assert speech_model.calls[0]["file"].name == "voice.mp3"
    assert speech_model.calls[0]["file"].read() == b"ID3voice"


def make_config(
    protocol: str = "openai",
    big_kind: str = "llm",
    organ_models: tuple[str, ...] = ("big", "small"),
    reasoning_effort: str | None = None,
    *,
    thinking: bool = False,
    web_search: bool = False,
    extra_params: dict[str, Any] | None = None,
    price_periods: list[dict[str, Any]] | None = None,
) -> ModelsConfig:
    organ: dict[str, Any] = {"models": list(organ_models), "max_tokens": 300}
    if reasoning_effort is not None:
        organ["reasoning_effort"] = reasoning_effort
    return ModelsConfig.model_validate(
        {
            "providers": [
                {
                    "name": "cloud",
                    "protocol": protocol,
                    "base_url": "https://api.example.com/v1",
                    "api_key_env": "AUREUS_KEY_CLOUD",
                },
                {"name": "local", "protocol": "openai"},
            ],
            "models": [
                {
                    "name": "big",
                    "identifier": "big-model",
                    "provider": "cloud",
                    "kind": big_kind,
                    "price_in": 4.0,
                    "price_out": 8.0,
                    "price_cache_in": 1.0,
                    "temperature": 0.9,
                    "max_tokens": 2000,
                    "reasoning_levels": ["high"],
                    "thinking": thinking,
                    "web_search": web_search,
                    "extra_params": extra_params or {},
                    "price_periods": price_periods or [],
                },
                {
                    "name": "small",
                    "identifier": "small-model",
                    "provider": "local",
                    "price_in": 0,
                    "price_out": 0,
                    "reasoning_levels": ["high"],
                },
            ],
            "organs": {"replyer": organ},
        }
    )


@pytest.fixture
def keys(tmp_path: Path) -> KeyResolver:
    return KeyResolver(tmp_path / ".env", {"AUREUS_KEY_CLOUD": CLOUD_SECRET})


# 假的计数工具固定返回这个数，测试结果不随 litellm 的分词表变化。
COUNTED_PROMPT_TOKENS = 50


def fixed_counter(messages: list[dict[str, str]]) -> int:
    return COUNTED_PROMPT_TOKENS


def make_organ(completion: FakeCompletion, keys: KeyResolver, **config_kwargs: Any) -> LLMOrgan:
    return LLMOrgan.from_config(
        "replyer", make_config(**config_kwargs), keys, completion, fixed_counter, SleepRecorder()
    )


def ask() -> OrganRequest:
    return OrganRequest(organ_id="replyer", prompt="你好")


READ_TOOL = ToolDefinition(
    "read", "查找资料", {"type": "object", "properties": {"query": {"type": "string"}}}
)
STOP_TOOL = ToolDefinition("stop", "结束本轮", {"type": "object", "properties": {}})


def make_planner(completion: FakeCompletion, keys: KeyResolver) -> LLMOrgan:
    return LLMOrgan("planner", make_config().routes_for("replyer"), keys, completion, fixed_counter)


def make_tool_fragment(
    arguments: str, *, call_id: str | None = None, name: str | None = None, index: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        index=index,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
async def test_planner_transports_tools_and_continues_with_a_correlated_tool_result(
    keys: KeyResolver,
) -> None:
    first = make_response(None, 10, 5, 2)
    first.choices[0].message.tool_calls = [
        make_tool_fragment('{"query": "资料"}', call_id="call_read", name="read")
    ]
    completion = FakeCompletion(first, make_response("普通文字"))
    organ = make_planner(completion, keys)
    request = OrganRequest("planner", "请查看资料", tools=(READ_TOOL,))

    response = await organ.process(request)

    assert response.suggestion == ""
    assert response.tool_calls == (ToolCall("read", "call_read", {"query": "资料"}),)
    assert response.attempts == [CallAttempt("big", "cloud", 10, 5, 2, cost=0.000074)]
    assert completion.calls[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "查找资料",
                "parameters": READ_TOOL.parameters,
            },
        }
    ]

    request.tool_history = (
        ToolExchange(response.tool_calls, (ToolResult("call_read", "找到一条合成资料"),)),
    )
    followup = await organ.process(request)

    messages = completion.calls[1]["messages"]
    assert messages[:-2] == completion.calls[0]["messages"]
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] is None
    assert messages[-2]["tool_calls"][0]["id"] == "call_read"
    function = messages[-2]["tool_calls"][0]["function"]
    assert function["name"] == "read"
    assert json.loads(function["arguments"]) == {"query": "资料"}
    assert messages[-1] == {
        "role": "tool",
        "tool_call_id": "call_read",
        "content": "找到一条合成资料",
    }
    assert followup.suggestion == "普通文字"
    assert followup.tool_calls == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [("read", "{"), ("read", "[]"), ("read", '{"query": NaN}'), ("unknown", "{}")],
)
async def test_malformed_tool_call_disables_the_entire_batch_and_keeps_usage(
    keys: KeyResolver, name: str, arguments: str
) -> None:
    raw = make_response("这不是聊天回复", 10, 5)
    raw.choices[0].message.tool_calls = [
        make_tool_fragment("{}", call_id="call_stop", name="stop"),
        make_tool_fragment(arguments, call_id="call_read", name=name),
    ]
    completion = FakeCompletion(raw)
    response = await make_planner(completion, keys).process(
        OrganRequest("planner", "规划材料", tools=(STOP_TOOL, READ_TOOL))
    )
    assert response.tool_calls == () and response.suggestion == ""
    assert response.metadata["tool_calls_invalid"] is True
    assert response.attempts[0].budget_tokens == 15
    assert len(completion.calls) == 1


@pytest.mark.asyncio
async def test_request_without_tools_does_not_forward_schema_or_expose_tool_calls(
    keys: KeyResolver,
) -> None:
    raw = make_response("照常回答")
    raw.choices[0].message.tool_calls = [make_tool_fragment("{}", call_id="call_stop", name="stop")]
    completion = FakeCompletion(raw)
    response = await make_organ(completion, keys).process(ask())
    assert "tools" not in completion.calls[0]
    assert response.suggestion == "照常回答"
    assert response.tool_calls == ()
    assert "tool_calls_invalid" not in response.metadata


@pytest.mark.asyncio
async def test_planner_stream_assembles_interleaved_tool_arguments_and_final_usage(
    keys: KeyResolver,
) -> None:
    async def chunks() -> AsyncIterator[Any]:
        for piece in (
            make_tool_fragment("{", name="stop", call_id="call_stop", index=1),
            make_tool_fragment('{"query":', name="read", call_id="call_read"),
            make_tool_fragment("}", index=1),
            make_tool_fragment('"资料"}'),
        ):
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[piece]))]
            )
        yield SimpleNamespace(choices=[SimpleNamespace(delta=None, finish_reason="tool_calls")])
        yield SimpleNamespace(choices=[], usage=make_response(None, 7, 1).usage)

    completion = FakeCompletion(chunks())
    response = await make_planner(completion, keys).process(
        OrganRequest("planner", "规划材料", tools=(READ_TOOL, STOP_TOOL), stream=True)
    )
    assert completion.calls[0]["stream_options"] == {"include_usage": True}
    assert response.suggestion == ""
    assert response.tool_calls == (
        ToolCall("read", "call_read", {"query": "资料"}),
        ToolCall("stop", "call_stop", {}),
    )
    assert response.attempts[0].budget_tokens == 8
    assert not response.attempts[0].estimated


@pytest.mark.asyncio
async def test_organ_assembles_stream_chunks_and_uses_final_usage(keys: KeyResolver) -> None:
    async def chunks() -> AsyncIterator[Any]:
        for fragment in ('{"reply":', "false}"):
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=fragment))]
            )
        yield SimpleNamespace(choices=[], usage=make_response(None, 7, 1).usage)

    completion = FakeCompletion(chunks())
    request = OrganRequest("replyer", "规划材料", stream=True)
    response = await make_organ(completion, keys).process(request)
    assert completion.calls[0]["stream"] is True
    assert response.suggestion == '{"reply":false}'
    assert response.attempts[0].budget_tokens == 8
    assert not response.attempts[0].estimated


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [(), (READ_TOOL,)])
async def test_cancelling_organ_stream_closes_it_and_accounts_for_attempt(
    keys: KeyResolver, tools: tuple[ToolDefinition, ...]
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    async def chunks() -> AsyncIterator[Any]:
        try:
            partial = make_tool_fragment("{", call_id="call_read", name="read")
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[partial]))]
            )
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    organ = make_organ(FakeCompletion(chunks()), keys)
    task = asyncio.create_task(
        organ.process(OrganRequest("replyer", "规划材料", stream=True, tools=tools))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(OrganCallCancelled) as cancellation:
        await task
    assert closed.is_set()
    [attempt] = cancellation.value.attempts
    assert attempt.estimated and attempt.budget_tokens > 0
    if tools:
        assert attempt.prompt_tokens > COUNTED_PROMPT_TOKENS
    else:
        assert attempt.prompt_tokens == COUNTED_PROMPT_TOKENS


def test_model_test_resources_match_maibot_snapshot() -> None:
    """提示词、工具和图片与固定提交 062d45c 的 MaiBot 源码一致（3.53）。"""
    assert _MODEL_TEST_IMAGE_BASE64 == (
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAAXNSR0IArs4c6QAAAARn"
        "QU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAABBSURBVFhH7c6hDQAgDEXB"
        "DstKjMkM4GsQBIq4Js9U/Fy0PmZlkR+vAwAA2AJOL+/lAAAAAAAAAP4H3A4AAKAcsAB+"
        "y6u1cyDAxAAAAABJRU5ErkJggg=="
    )
    without_image = (
        "你正在执行 MaiBot WebUI 的单模型能力测试。\n"
        "测试目标：确认模型可以读取普通文本，并可以按工具定义发起 tool call。\n"
        "本次消息没有附带图片，请在工具参数 saw_image 中填 false。\n"
        "请必须调用工具 maibot_model_test_report，不要只用普通文本回答。\n"
        "工具参数要求：status 填 ok，echo 填 maibot model test。"
    )
    with_image = (
        "你正在执行 MaiBot WebUI 的单模型能力测试。\n"
        "测试目标：确认模型可以读取普通文本，并可以按工具定义发起 tool call。\n"
        "本次消息还附带了一张测试图片，请在工具参数 saw_image 中填 true，"
        "并简要描述图片。\n"
        "请必须调用工具 maibot_model_test_report，不要只用普通文本回答。\n"
        "工具参数要求：status 填 ok，echo 填 maibot model test。"
    )
    assert _model_test_prompt(with_image=False) == without_image
    assert _model_test_prompt(with_image=True) == with_image
    assert _model_test_tool() == {
        "type": "function",
        "function": {
            "name": "maibot_model_test_report",
            "description": "回报 MaiBot WebUI 模型测试结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "测试状态，成功时填 ok。",
                        "enum": ["ok"],
                    },
                    "echo": {
                        "type": "string",
                        "description": "文本回显，固定填 maibot model test。",
                    },
                    "saw_image": {
                        "type": "boolean",
                        "description": "本次请求中是否包含并识别了测试图片。",
                    },
                    "image_summary": {
                        "type": "string",
                        "description": "如果包含图片，简短描述图片内容；未包含图片时留空。",
                    },
                },
                "required": ["status", "echo", "saw_image"],
            },
        },
    }
    assert _MODEL_TEST_TOOL_NAME == "maibot_model_test_report"


def test_model_probe_inputs_use_maibot_prompt_image_and_tool() -> None:
    model = ModelConfig(
        name="vision",
        identifier="vision",
        provider="test",
        vision=True,
        reasoning_levels=("high",),
    )
    probes = model_probe_inputs(model)
    message = {"role": "user", "content": _model_test_prompt(with_image=False)}
    assert probes["text"] == {"messages": [message], "tools": [_model_test_tool()]}
    assert probes["tools"] == {"messages": [message], "tools": [_model_test_tool()]}
    assert probes["vision"] == {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _model_test_prompt(with_image=True)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + _MODEL_TEST_IMAGE_BASE64
                        },
                    },
                ],
            }
        ],
        "tools": [_model_test_tool()],
    }
    assert probes["reasoning/high"] == {
        "messages": [message],
        "tools": [_model_test_tool()],
        "reasoning_effort": "high",
    }
    image_model = ModelConfig(
        name="picture",
        identifier="picture",
        provider="test",
        kind="image_embedding",
    )
    assert model_probe_inputs(image_model)["image_embedding"]["input"] == [
        {"image": _MODEL_TEST_IMAGE_BASE64}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", LITELLM_PREFIX_BY_PROTOCOL)
async def test_model_probe_stream_records_visible_text_and_final_usage(
    keys: KeyResolver, protocol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield SimpleNamespace(choices=[])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=None)])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="OK"))])
        yield SimpleNamespace(choices=[], usage=make_response(None, 7, 1).usage)

    completion = FakeCompletion(chunks())
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config(protocol=protocol)
    result = await probe_model(config.models[0], config.providers[0], keys)
    assert result.metadata["latency_ms"] >= 0
    assert result.metadata["kind"] == "llm"
    assert result.attempts[0].budget_tokens == 8
    assert not result.attempts[0].estimated
    assert completion.calls[0]["stream"] is True
    assert completion.calls[0]["num_retries"] == 0
    assert (
        completion.calls[0]["model"]
        == LITELLM_PREFIX_BY_PROTOCOL[config.providers[0].protocol] + "big-model"
    )
    assert len(completion.calls) == 1


@pytest.mark.asyncio
async def test_text_probe_accepts_a_streamed_test_tool_call(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def chunks() -> AsyncIterator[Any]:
        delta = SimpleNamespace(
            content=None,
            tool_calls=[
                make_tool_fragment(
                    '{"status": "ok", "echo": "maibot model test", "saw_image": false}',
                    call_id="call_test",
                    name=_MODEL_TEST_TOOL_NAME,
                )
            ],
        )
        yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])
        yield SimpleNamespace(choices=[])

    completion = FakeCompletion(chunks())
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    result = await probe_model(config.models[0], config.providers[0], keys)

    assert json.loads(result.suggestion) == [
        {
            "name": _MODEL_TEST_TOOL_NAME,
            "arguments": {
                "status": "ok",
                "echo": "maibot model test",
                "saw_image": False,
            },
        }
    ]
    assert result.metadata["latency_ms"] >= 0
    assert len(completion.calls) == 1


@pytest.mark.asyncio
async def test_model_probe_closes_failed_stream_and_sanitizes_error(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = False

    async def chunks() -> AsyncIterator[Any]:
        nonlocal closed
        try:
            yield SimpleNamespace(choices=[])
            raise Timeout(CLOUD_SECRET)
        finally:
            closed = True

    completion = FakeCompletion(chunks())
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    with pytest.raises(OrganCallError, match="超时") as error:
        await probe_model(config.models[0], config.providers[0], keys)
    assert CLOUD_SECRET not in str(error.value)
    assert error.value.attempts[0].estimated
    assert closed


def make_stream_chunk(*, content: str | None = None, usage: Any = None) -> SimpleNamespace:
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])
    if usage is not None:
        chunk.usage = usage
    return chunk


@pytest.mark.asyncio
async def test_probe_falls_back_to_non_streaming_when_stream_has_no_content(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield make_stream_chunk(content=None)
        yield SimpleNamespace(choices=[], usage=make_response(None, 7, 1).usage)

    completion = FakeCompletion(chunks(), make_response("OK", 8, 2))
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    result = await probe_model(config.models[0], config.providers[0], keys)

    assert result.suggestion == "OK"
    assert len(completion.calls) == 2
    assert completion.calls[0]["stream"] is True
    assert "stream" not in completion.calls[1]
    assert completion.calls[1]["messages"] == completion.calls[0]["messages"]
    assert completion.calls[1]["max_tokens"] == completion.calls[0]["max_tokens"]
    assert result.attempts[0].succeeded is False
    assert result.attempts[0].failure_reason == EMPTY_REPLY_REASON
    assert result.attempts[0].budget_tokens == 8
    assert result.attempts[-1].succeeded is True
    assert result.attempts[-1].budget_tokens == 10  # 用量以非流式响应为准
    assert result.metadata["latency_ms"] >= 0


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
@pytest.mark.asyncio
async def test_probe_accepts_reasoning_only_and_says_so(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    async def chunks() -> AsyncIterator[Any]:
        delta = SimpleNamespace(content=None)
        setattr(delta, field, "先想一想")
        yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])
        yield SimpleNamespace(choices=[])

    fallback = make_response(None, 100, 20)
    setattr(fallback.choices[0].message, field, "再想一想")
    completion = FakeCompletion(chunks(), fallback)
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    result = await probe_model(config.models[0], config.providers[0], keys)

    assert result.suggestion == REASONING_ONLY_REASON
    assert len(completion.calls) == 2
    assert result.attempts[0].failure_reason == REASONING_ONLY_REASON
    assert result.attempts[-1].succeeded is True
    assert result.attempts[-1].budget_tokens == 120
    assert result.metadata["latency_ms"] >= 0


LOCAL_EMBEDDING_URL = "http://127.0.0.1:12345/v1"


class FakeEmbeddingClock:
    """嵌入请求的假时钟：只有客户端创建和请求本身会推进它（D128）。"""

    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds


def local_embedding_route() -> ModelRoute:
    provider = ProviderConfig(name="local", protocol="openai", base_url=LOCAL_EMBEDDING_URL)
    model = ModelConfig(
        name="local-embed", identifier="local-embed", provider="local", kind="embedding"
    )
    return ModelRoute(model, provider, None, None)


@pytest.mark.asyncio
async def test_local_embedding_probe_uses_direct_client(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_import() -> Any:
        raise AssertionError("本地嵌入不应导入 litellm")

    monkeypatch.setattr(llm_adapter, "_import_litellm", fail_import)
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == LOCAL_EMBEDDING_URL + "/embeddings"
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.5, 0.25]}], "usage": {"prompt_tokens": 3}},
        )

    route = local_embedding_route()
    response = await probe_model(
        route.model, route.provider, keys, transport=httpx.MockTransport(respond)
    )

    assert response.suggestion == "[0.5, 0.25]"
    assert response.metadata["kind"] == "embedding"
    assert response.attempts[0].prompt_tokens == 3
    assert bodies == [{"model": "local-embed", "input": [_MODEL_TEST_INPUT]}]
    await close_shared_clients()


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("connect", "连不上服务商"),
        ("status", "服务商内部错误"),
        ("json", "返回内容为空或格式异常"),
    ],
)
@pytest.mark.asyncio
async def test_local_embedding_probe_failures_are_reported(
    keys: KeyResolver, mode: str, reason: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if mode == "connect":
            raise httpx.ConnectError("连不上", request=request)
        if mode == "status":
            return httpx.Response(500, json={})
        return httpx.Response(200, text="不是 JSON")

    route = local_embedding_route()
    with pytest.raises(OrganCallError, match=reason) as error:
        await probe_model(
            route.model, route.provider, keys, transport=httpx.MockTransport(respond)
        )

    assert error.value.attempts[0].succeeded is False
    assert error.value.attempts[0].failure_reason == reason
    await close_shared_clients()


@pytest.mark.asyncio
async def test_probe_latency_excludes_client_creation(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeEmbeddingClock()
    monkeypatch.setattr(llm_adapter, "_request_time", clock)

    class SlowClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            clock.seconds += 0.5
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(llm_adapter.httpx, "AsyncClient", SlowClient)

    def respond(request: httpx.Request) -> httpx.Response:
        clock.seconds += 0.007
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    route = local_embedding_route()
    response = await probe_model(
        route.model, route.provider, keys, transport=httpx.MockTransport(respond)
    )

    assert response.metadata["latency_ms"] == 7
    await close_shared_clients()


@pytest.mark.asyncio
async def test_embedding_client_is_reused_between_calls(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    created: list[httpx.AsyncClient] = []

    class CountingClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(llm_adapter.httpx, "AsyncClient", CountingClient)
    organ = EmbeddingOrgan(
        "text_embedding",
        [local_embedding_route()],
        keys,
        transport=httpx.MockTransport(respond),
    )
    request = OrganRequest("text_embedding", "", embedding_inputs=("甲",))
    await organ.process(request)
    await organ.process(request)

    assert len(created) == 1
    assert len(requests) == 2
    await close_shared_clients()


@pytest.mark.asyncio
async def test_embedding_client_closes_on_shutdown(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[httpx.AsyncClient] = []

    class CountingClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(llm_adapter.httpx, "AsyncClient", CountingClient)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    organ = EmbeddingOrgan(
        "text_embedding",
        [local_embedding_route()],
        keys,
        transport=httpx.MockTransport(respond),
    )
    request = OrganRequest("text_embedding", "", embedding_inputs=("甲",))
    await organ.process(request)
    assert len(created) == 1
    assert not created[0].is_closed

    await close_shared_clients()

    assert created[0].is_closed
    await organ.process(request)
    assert len(created) == 2
    await close_shared_clients()


@pytest.mark.asyncio
async def test_remote_embedding_still_uses_litellm(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = SimpleNamespace(data=[{"embedding": [0.1]}], usage=make_response(None, 5, 0).usage)
    embedding = FakeCompletion(result)
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(aembedding=embedding)
    )
    config = make_config(protocol="openai_responses", big_kind="embedding", organ_models=("small",))
    response = await probe_model(config.models[0], config.providers[0], keys)
    assert response.metadata["kind"] == "embedding"
    assert response.metadata["latency_ms"] >= 0
    assert response.attempts[0].budget_tokens == 5
    assert embedding.calls[0]["model"] == "openai/big-model"
    assert len(embedding.calls[0]["input"]) == 1


LOCAL_CHAT_URL = "http://127.0.0.1:12345/v1"


def local_chat_route(
    *,
    vision: bool = False,
    prices: bool = False,
    reasoning: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: ReasoningLevel | None = None,
    thinking: bool = False,
    extra_params: dict[str, Any] | None = None,
) -> ModelRoute:
    provider = ProviderConfig(name="local-chat", protocol="openai", base_url=LOCAL_CHAT_URL)
    model = ModelConfig(
        name="local-chat",
        identifier="magpie-chat",
        provider="local-chat",
        vision=vision,
        price_in=4.0 if prices else None,
        price_out=8.0 if prices else None,
        price_cache_in=1.0 if prices else None,
        reasoning_levels=("high",) if reasoning else (),
        thinking=thinking,
        extra_params=extra_params or {},
    )
    return ModelRoute(model, provider, temperature, max_tokens, reasoning_effort)


def local_chat_organ(
    keys: KeyResolver,
    transport: httpx.AsyncBaseTransport,
    *,
    organ_id: str = "replyer",
    **route_kwargs: Any,
) -> LLMOrgan:
    return LLMOrgan(
        organ_id,
        [local_chat_route(**route_kwargs)],
        keys,
        FakeCompletion(),
        fixed_counter,
        SleepRecorder(),
        transport=transport,
    )


def sse_response(*chunks: dict[str, Any]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        content=(body + "data: [DONE]\n\n").encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.asyncio
async def test_local_chat_probe_uses_direct_http(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_import() -> Any:
        raise AssertionError("本地对话不应导入 litellm")

    monkeypatch.setattr(llm_adapter, "_import_litellm", fail_import)
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == LOCAL_CHAT_URL + "/chat/completions"
        bodies.append(json.loads(request.content))
        return sse_response({"choices": [{"delta": {"content": "OK"}}]})

    route = local_chat_route()
    result = await probe_model(
        route.model, route.provider, keys, transport=httpx.MockTransport(respond)
    )

    assert result.suggestion == "OK"
    assert result.metadata["latency_ms"] >= 0
    assert bodies == [
        {
            "model": "magpie-chat",
            "messages": [
                {"role": "user", "content": _model_test_prompt(with_image=False)}
            ],
            "tools": [_model_test_tool()],
            "max_tokens": 512,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ]
    await close_shared_clients()


@pytest.mark.asyncio
async def test_vision_probe_defaults_to_non_streaming(keys: KeyResolver) -> None:
    # 视觉不需要首包观察，默认非流式（D132）；提示词、图片和判定按 MaiBot（3.53）。
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_test",
                                    "type": "function",
                                    "function": {
                                        "name": _MODEL_TEST_TOOL_NAME,
                                        "arguments": (
                                            '{"status": "ok", "echo": "maibot model test",'
                                            ' "saw_image": true}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    route = local_chat_route(vision=True)
    result = await probe_model(
        route.model,
        route.provider,
        keys,
        capability="vision",
        transport=httpx.MockTransport(respond),
    )

    assert result.attempts[0].succeeded is True
    assert json.loads(result.suggestion) == [
        {
            "name": _MODEL_TEST_TOOL_NAME,
            "arguments": {
                "status": "ok",
                "echo": "maibot model test",
                "saw_image": True,
            },
        }
    ]
    body = bodies[0]
    assert "stream" not in body
    assert body["tools"] == [_model_test_tool()]
    assert body["messages"][0]["content"][0] == {
        "type": "text",
        "text": _model_test_prompt(with_image=True),
    }
    assert body["messages"][0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + _MODEL_TEST_IMAGE_BASE64},
    }
    await close_shared_clients()


@pytest.mark.asyncio
async def test_vision_probe_requires_the_maibot_test_tool(keys: KeyResolver) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "一张蓝色图片"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    route = local_chat_route(vision=True)
    with pytest.raises(ModelProbeError, match=MODEL_TEST_TOOL_MISSING_REASON) as error:
        await probe_model(
            route.model,
            route.provider,
            keys,
            capability="vision",
            transport=httpx.MockTransport(respond),
        )

    assert error.value.reply == "一张蓝色图片"
    await close_shared_clients()


@pytest.mark.asyncio
async def test_tool_probe_uses_the_shared_tool_parsing(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 模型测试和普通调用共用同一套工具解析（D132）；参数不校验（3.53）。
    raw = make_response(None, 10, 5)
    raw.choices[0].message.tool_calls = [
        make_tool_fragment(
            '{"status": "ok", "echo": "maibot model test", "saw_image": false, "extra": 1}',
            call_id="call_test",
            name=_MODEL_TEST_TOOL_NAME,
        )
    ]
    completion = FakeCompletion(raw)
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    result = await probe_model(
        config.models[0], config.providers[0], keys, capability="tools"
    )

    assert json.loads(result.suggestion) == [
        {
            "name": _MODEL_TEST_TOOL_NAME,
            "arguments": {
                "status": "ok",
                "echo": "maibot model test",
                "saw_image": False,
                "extra": 1,
            },
        }
    ]
    assert result.attempts[0].succeeded is True
    assert result.attempts[0].budget_tokens == 15
    assert completion.calls[0]["tools"] == [_model_test_tool()]


@pytest.mark.asyncio
async def test_tool_probe_requires_the_maibot_test_tool(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = make_response(None, 10, 5)
    raw.choices[0].message.tool_calls = [
        make_tool_fragment('{"value": "OK"}', call_id="call_echo", name="echo")
    ]
    completion = FakeCompletion(raw)
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    with pytest.raises(ModelProbeError, match=MODEL_TEST_TOOL_MISSING_REASON):
        await probe_model(
            config.models[0], config.providers[0], keys, capability="tools"
        )


@pytest.mark.asyncio
async def test_reasoning_probe_requires_the_maibot_test_tool(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = make_response(None, 10, 5)
    raw.choices[0].message.tool_calls = [
        make_tool_fragment(
            '{"status": "ok", "echo": "maibot model test", "saw_image": false}',
            call_id="call_test",
            name=_MODEL_TEST_TOOL_NAME,
        )
    ]
    raw.choices[0].message.reasoning_content = "先想一想"
    completion = FakeCompletion(raw)
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    result = await probe_model(
        config.models[0], config.providers[0], keys, capability="reasoning/high"
    )

    assert result.attempts[0].succeeded is True
    assert completion.calls[0]["reasoning_effort"] == "high"
    assert completion.calls[0]["tools"] == [_model_test_tool()]
    assert "stream" not in completion.calls[0]


@pytest.mark.asyncio
async def test_reasoning_probe_rejects_reasoning_without_tool_call(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = make_response(None, 10, 5)
    raw.choices[0].message.reasoning_content = "只想不做"
    completion = FakeCompletion(raw)
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    with pytest.raises(ModelProbeError, match=MODEL_TEST_TOOL_MISSING_REASON) as error:
        await probe_model(
            config.models[0], config.providers[0], keys, capability="reasoning/high"
        )

    assert error.value.reply == REASONING_ONLY_REASON


class HangingStream(httpx.AsyncByteStream):
    """一直不返回数据的响应体，模拟不结束的本地网关流（D131）。"""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""


@pytest.mark.asyncio
async def test_probe_first_chunk_timeout_falls_back_to_non_streaming(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_adapter, "PROBE_FIRST_CHUNK_TIMEOUT_SECONDS", 0.05)
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if bodies[-1].get("stream"):
            return httpx.Response(
                200,
                stream=HangingStream(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2},
            },
        )

    route = local_chat_route()
    result = await probe_model(
        route.model, route.provider, keys, transport=httpx.MockTransport(respond)
    )

    assert result.suggestion == "OK"
    assert len(bodies) == 2
    assert bodies[0]["stream"] is True
    assert "stream" not in bodies[1]
    assert bodies[1]["messages"] == bodies[0]["messages"]
    assert result.attempts[0].succeeded is False
    assert result.attempts[0].failure_reason == "超时"
    assert result.attempts[0].estimated is True
    assert result.attempts[-1].succeeded is True
    assert result.attempts[-1].budget_tokens == 10
    assert result.metadata["latency_ms"] >= 0
    await close_shared_clients()


@pytest.mark.asyncio
async def test_probe_first_chunk_with_data_does_not_fall_back(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_adapter, "PROBE_FIRST_CHUNK_TIMEOUT_SECONDS", 0.05)

    async def partial() -> AsyncIterator[Any]:
        yield make_stream_chunk(content="O")
        await asyncio.sleep(0.1)  # 比首包超时更久：首包之后不再计时
        raise Timeout("流断了")

    completion = FakeCompletion(partial(), make_response("OK", 8, 2))
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    with pytest.raises(OrganCallError, match="超时"):
        await probe_model(config.models[0], config.providers[0], keys)

    assert len(completion.calls) == 1  # 已经有数据，不再回退非流式
    assert completion.calls[0]["stream"] is True


@pytest.mark.asyncio
async def test_local_chat_supports_tools_and_vision(keys: KeyResolver) -> None:
    bodies: list[dict[str, Any]] = []
    replies = [
        {
            "choices": [{"message": {"content": "red"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        },
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_read",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"query": "资料"}'},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(bodies) - 1])

    transport = httpx.MockTransport(respond)
    vlm = local_chat_organ(
        keys,
        transport,
        organ_id="vlm",
        vision=True,
        reasoning=True,
        temperature=0.7,
        max_tokens=300,
        reasoning_effort="high",
    )
    response = await vlm.process(
        OrganRequest("vlm", "描述图片", media_bytes=b"abc", media_type="image/png")
    )

    assert response.suggestion == "red"
    vision_body = bodies[0]
    assert vision_body["model"] == "magpie-chat"
    assert vision_body["temperature"] == 0.7
    assert vision_body["max_tokens"] == 300
    assert vision_body["reasoning_effort"] == "high"
    assert "stream" not in vision_body
    assert vision_body["messages"][-1]["content"][-1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,YWJj"},
    }

    planner = local_chat_organ(keys, transport, organ_id="planner", max_tokens=100)
    planned = await planner.process(OrganRequest("planner", "规划材料", tools=(READ_TOOL,)))

    assert planned.tool_calls == (ToolCall("read", "call_read", {"query": "资料"}),)
    tool_body = bodies[1]
    assert tool_body["model"] == "magpie-chat"
    assert tool_body["max_tokens"] == 100
    assert tool_body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "查找资料",
                "parameters": READ_TOOL.parameters,
            },
        }
    ]
    await close_shared_clients()


@pytest.mark.asyncio
async def test_local_chat_sends_thinking_and_extra_params_but_not_client_options(
    keys: KeyResolver,
) -> None:
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "嗨"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    organ = local_chat_organ(
        keys,
        httpx.MockTransport(respond),
        thinking=True,
        extra_params={"top_p": 0.8},
    )
    response = await organ.process(ask())

    assert response.suggestion == "嗨"
    body = bodies[0]
    assert body["thinking"] == {"type": "enabled"}
    assert body["top_p"] == 0.8
    assert "timeout" not in body
    assert "num_retries" not in body
    assert "api_key" not in body
    await close_shared_clients()


@pytest.mark.asyncio
async def test_local_chat_usage_and_failures_match_litellm_mapping(keys: KeyResolver) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "嗨"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            },
        )

    organ = local_chat_organ(
        keys, httpx.MockTransport(respond), prices=True, max_tokens=300
    )
    response = await organ.process(ask())

    assert response.suggestion == "嗨"
    assert response.metadata["usage_known"] is True
    assert response.metadata["prompt_tokens"] == 100
    assert response.metadata["completion_tokens"] == 20
    assert response.metadata["cached_tokens"] == 40
    assert response.metadata["cost"] == pytest.approx(0.00044)

    stream_bodies: list[dict[str, Any]] = []

    def stream_respond(request: httpx.Request) -> httpx.Response:
        stream_bodies.append(json.loads(request.content))
        return sse_response(
            {"choices": [{"delta": {"content": "嗨"}}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 1}},
        )

    streaming = local_chat_organ(keys, httpx.MockTransport(stream_respond))
    streamed = await streaming.process(OrganRequest("replyer", "你好", stream=True))

    assert streamed.suggestion == "嗨"
    assert streamed.metadata["usage_known"] is True
    assert streamed.metadata["prompt_tokens"] == 7
    assert streamed.metadata["completion_tokens"] == 1
    assert stream_bodies[0]["stream"] is True
    assert stream_bodies[0]["stream_options"] == {"include_usage": True}

    for status, reason, rejected, attempts in (
        (401, "认证失败（密钥无效？）", True, 1),
        (429, "被限流", True, 1),
        (500, "服务商内部错误", False, 2),
    ):

        def fail(request: httpx.Request, status: int = status) -> httpx.Response:
            return httpx.Response(status, json={"error": "x"})

        failing = local_chat_organ(keys, httpx.MockTransport(fail))
        with pytest.raises(OrganCallError, match=reason) as error:
            await failing.process(ask())
        assert len(error.value.attempts) == attempts
        for attempt in error.value.attempts:
            assert attempt.failure_reason == reason
            assert attempt.estimated is (not rejected)
            if rejected:
                assert attempt.budget_tokens == 0
    await close_shared_clients()


@pytest.mark.asyncio
async def test_chat_defaults_to_non_streaming(keys: KeyResolver) -> None:
    # 普通对话默认非流式（D132），请求体里没有 stream / stream_options。
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "嗨"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    organ = local_chat_organ(keys, httpx.MockTransport(respond))
    response = await organ.process(ask())

    assert response.suggestion == "嗨"
    assert len(bodies) == 1
    assert "stream" not in bodies[0]
    assert "stream_options" not in bodies[0]
    await close_shared_clients()


@pytest.mark.asyncio
async def test_local_calls_stay_direct_and_proxy_free(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 本地回环直连：不走 litellm、不读环境代理，并复用同一个客户端（D131、D132）。
    requests: list[httpx.Request] = []
    created: list[tuple[httpx.AsyncClient, bool]] = []

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created.append((self, bool(kwargs.get("trust_env"))))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(llm_adapter.httpx, "AsyncClient", RecordingClient)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "嗨"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    completion = FakeCompletion()
    organ = LLMOrgan(
        "replyer",
        [local_chat_route()],
        keys,
        completion,
        fixed_counter,
        transport=httpx.MockTransport(respond),
    )
    await organ.process(ask())
    await organ.process(ask())

    assert completion.calls == []  # 本地直连不经过 litellm
    assert len(created) == 1  # 两次调用复用同一个客户端
    assert created[0][1] is False  # trust_env=False：不读环境代理
    assert {str(request.url) for request in requests} == {
        LOCAL_CHAT_URL + "/chat/completions"
    }
    await close_shared_clients()


@pytest.mark.asyncio
async def test_local_chat_stream_empty_reply_falls_back_to_non_streaming(
    keys: KeyResolver,
) -> None:
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if bodies[-1].get("stream"):
            return sse_response({"choices": [{"delta": {"content": None}}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "备用"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    organ = local_chat_organ(keys, httpx.MockTransport(respond))
    response = await organ.process(OrganRequest("replyer", "你好", stream=True))

    assert response.suggestion == "备用"
    assert len(bodies) == 2
    assert bodies[0]["stream"] is True
    assert "stream" not in bodies[1]
    assert bodies[1]["messages"] == bodies[0]["messages"]
    assert [attempt.succeeded for attempt in response.attempts] == [False, True]
    assert response.attempts[0].failure_reason == EMPTY_REPLY_REASON
    await close_shared_clients()


@pytest.mark.asyncio
async def test_remote_calls_reuse_one_client(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 远程服务商共用同一个带连接池的客户端，沿用环境代理设置（D132）。
    created: list[tuple[httpx.AsyncClient, bool]] = []

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created.append((self, bool(kwargs.get("trust_env"))))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(llm_adapter.httpx, "AsyncClient", RecordingClient)

    async def chunks() -> AsyncIterator[Any]:
        yield make_stream_chunk(content="远程")

    library = ModuleType("litellm")
    library.acompletion = FakeCompletion(chunks(), chunks())  # type: ignore[attr-defined]
    monkeypatch.setattr(llm_adapter, "_import_litellm", lambda: library)
    config = make_config()
    route = ModelRoute(config.models[0], config.providers[0], None, None)

    await probe_model(route.model, route.provider, keys)
    await probe_model(route.model, route.provider, keys)

    assert len(created) == 1  # 两次远程调用复用同一个客户端
    assert created[0][1] is True  # trust_env=True：沿用环境代理设置
    assert library.aclient_session is created[0][0]  # type: ignore[attr-defined]
    await close_shared_clients()


@pytest.mark.asyncio
async def test_remote_chat_still_uses_litellm(
    keys: KeyResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield make_stream_chunk(content="远程")

    completion = FakeCompletion(chunks())
    monkeypatch.setattr(
        llm_adapter, "_import_litellm", lambda: SimpleNamespace(acompletion=completion)
    )
    config = make_config()
    result = await probe_model(config.models[0], config.providers[0], keys)

    assert result.suggestion == "远程"
    assert completion.calls[0]["model"] == "openai/big-model"
    assert completion.calls[0]["api_base"] == "https://api.example.com/v1"
    assert completion.calls[0]["stream"] is True


@pytest.mark.asyncio
async def test_first_model_success_passes_the_configured_call_arguments(keys: KeyResolver) -> None:
    completion = FakeCompletion(make_response("嗨", prompt=1000, completion=100, cached=400))
    response = await make_organ(completion, keys).process(ask())

    assert response.suggestion == "嗨"
    assert len(completion.calls) == 1
    call = completion.calls[0]
    assert call["model"] == "openai/big-model"
    assert call["api_base"] == "https://api.example.com/v1"
    assert call["api_key"] == CLOUD_SECRET
    assert call["timeout"] == 90.0
    assert call["num_retries"] == 0  # 重试由适配器自己做，每次请求都记账（审查 H4）
    assert call["temperature"] == 0.9  # 模型自己的
    assert call["max_tokens"] == 300  # 器官上的，优先于模型的 2000
    assert "reasoning_effort" not in call
    assert [m["role"] for m in call["messages"]] == ["system", "user"]
    assert call["messages"][1]["content"] == "你好"
    assert response.metadata["model"] == "big"
    assert response.metadata["cost"] == pytest.approx(0.0036)
    assert response.metadata["cached_tokens"] == 400


@pytest.mark.asyncio
async def test_configured_reasoning_effort_is_passed_to_litellm(keys: KeyResolver) -> None:
    completion = FakeCompletion(make_response("嗨"))
    await make_organ(completion, keys, reasoning_effort="high").process(ask())

    assert completion.calls[0]["reasoning_effort"] == "high"
    assert completion.calls[0]["temperature"] == 0.9
    assert completion.calls[0]["max_tokens"] == 300


@pytest.mark.asyncio
async def test_thinking_and_extra_params_are_passed_to_litellm(keys: KeyResolver) -> None:
    completion = FakeCompletion(make_response("嗨"))
    organ = make_organ(
        completion,
        keys,
        thinking=True,
        extra_params={"top_p": 0.8, "stop": ["再见"]},
    )
    await organ.process(ask())

    call = completion.calls[0]
    assert call["thinking"] == {"type": "enabled"}
    assert call["top_p"] == 0.8
    assert call["stop"] == ["再见"]


@pytest.mark.asyncio
async def test_explicit_fields_win_over_extra_params_and_warn_once(keys: KeyResolver) -> None:
    completion = FakeCompletion(make_response("嗨"), make_response("嗨"))
    organ = make_organ(
        completion,
        keys,
        extra_params={"temperature": 0.1, "max_tokens": 1, "top_p": 0.8},
    )
    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(str(message)), level="WARNING", format="{message}"
    )
    try:
        await organ.process(ask())
        await organ.process(ask())
    finally:
        logger.remove(sink_id)

    call = completion.calls[0]
    assert call["temperature"] == 0.9
    assert call["max_tokens"] == 300
    assert call["top_p"] == 0.8
    assert sum("temperature" in message for message in messages) == 1
    assert sum("max_tokens" in message for message in messages) == 1


@pytest.mark.asyncio
async def test_organ_uses_the_clock_to_choose_period_prices(keys: KeyResolver) -> None:
    clock = ManualClock(datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
    config = make_config(
        price_periods=[
            {
                "start": "00:00",
                "end": "24:00",
                "price_in": 10.0,
                "price_out": 20.0,
                "price_cache_in": 5.0,
            }
        ]
    )
    completion = FakeCompletion(make_response("嗨", prompt=1000, completion=100))
    organ = LLMOrgan.from_config(
        "replyer", config, keys, completion, fixed_counter, SleepRecorder(), clock=clock
    )
    response = await organ.process(ask())

    assert response.metadata["cost"] == pytest.approx(0.012)


@pytest.mark.asyncio
async def test_web_search_is_added_only_for_the_responses_protocol(keys: KeyResolver) -> None:
    supported = FakeCompletion(make_response("嗨"))
    await make_organ(
        supported, keys, protocol="openai_responses", web_search=True
    ).process(ask())
    assert supported.calls[0]["tools"] == [{"type": "web_search"}]

    unsupported = FakeCompletion(make_response("嗨"), make_response("嗨"))
    organ = make_organ(unsupported, keys, protocol="openai", web_search=True)
    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(str(message)), level="WARNING", format="{message}"
    )
    try:
        await organ.process(ask())
        await organ.process(ask())
    finally:
        logger.remove(sink_id)

    assert "tools" not in unsupported.calls[0]
    assert sum("联网搜索" in message for message in messages) == 1


@pytest.mark.parametrize("protocol", list(LITELLM_PREFIX_BY_PROTOCOL))
@pytest.mark.asyncio
async def test_protocol_selects_the_litellm_model_prefix(keys: KeyResolver, protocol: str) -> None:
    completion = FakeCompletion(make_response("嗨"))
    await make_organ(completion, keys, protocol=protocol).process(ask())
    assert completion.calls[0]["model"] == LITELLM_PREFIX_BY_PROTOCOL[protocol] + "big-model"  # type: ignore[index]


@pytest.mark.asyncio
async def test_failed_model_falls_through_to_the_next(keys: KeyResolver) -> None:
    # 限流不重试（D132），大模型失败一次就顺延到小模型。
    completion = FakeCompletion(RateLimitError(f"限流 {CLOUD_SECRET}"), make_response("备用"))
    response = await make_organ(completion, keys).process(ask())

    assert response.suggestion == "备用"
    assert response.metadata["model"] == "small"
    assert response.metadata["failures_before_success"] == ["big：被限流"]
    # 本地服务商不需要密钥：不带 api_key，也不带云端的地址。
    assert "api_key" not in completion.calls[1]
    assert "api_base" not in completion.calls[1]


@pytest.mark.asyncio
async def test_all_models_failing_raises_without_leaking_the_secret(keys: KeyResolver) -> None:
    # 限流不重试，超时重试一次（D132）：大模型 1 次、小模型 2 次。
    completion = FakeCompletion(RateLimitError(CLOUD_SECRET), *[Timeout("x")] * 2)
    with pytest.raises(OrganCallError) as info:
        await make_organ(completion, keys).process(ask())

    message = str(info.value)
    assert "big：被限流" in message
    assert "small：超时" in message
    assert CLOUD_SECRET not in message
    assert info.value.organ_id == "replyer"


@pytest.mark.asyncio
async def test_unknown_error_type_is_reported_by_class_name_only(keys: KeyResolver) -> None:
    completion = FakeCompletion(ValueError(CLOUD_SECRET), ValueError(CLOUD_SECRET))
    with pytest.raises(OrganCallError) as info:
        await make_organ(completion, keys).process(ask())
    assert "ValueError" in str(info.value)
    assert CLOUD_SECRET not in str(info.value)


@pytest.mark.asyncio
async def test_missing_key_skips_the_model_without_sending_a_request(tmp_path: Path) -> None:
    no_keys = KeyResolver(tmp_path / "absent.env", {})
    completion = FakeCompletion(make_response("备用"))
    response = await make_organ(completion, no_keys).process(ask())

    assert len(completion.calls) == 1  # 云端那个根本没有发出请求
    assert response.metadata["model"] == "small"
    assert "密钥缺失" in response.metadata["failures_before_success"][0]


@pytest.mark.asyncio
async def test_empty_reply_still_records_the_cost_it_incurred(keys: KeyResolver) -> None:
    # 推理模型把 token 都花在思考上，内容为空，但这笔钱已经花了。
    # 空响应重试一次（D132），两次都失败才顺延到小模型。
    wasted = make_response("  ", prompt=1000, completion=500)
    completion = FakeCompletion(wasted, wasted, make_response("备用"))
    response = await make_organ(completion, keys).process(ask())

    assert response.metadata["wasted_cost"] == pytest.approx(0.016)
    assert response.metadata["cost"] == 0.0
    assert EMPTY_REPLY_REASON in response.metadata["failures_before_success"][0]


@pytest.mark.parametrize(
    "bad",
    [
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None))]),
        SimpleNamespace(),
    ],
)
@pytest.mark.asyncio
async def test_malformed_replies_count_as_failures(keys: KeyResolver, bad: Any) -> None:
    # 两个模型各重试一次空响应（D132）。
    completion = FakeCompletion(bad, bad, bad, bad)
    with pytest.raises(OrganCallError, match=EMPTY_REPLY_REASON):
        await make_organ(completion, keys).process(ask())


@pytest.mark.asyncio
async def test_wasted_cost_is_carried_by_the_final_error(keys: KeyResolver) -> None:
    wasted = make_response(None, prompt=1000, completion=500)
    with pytest.raises(OrganCallError) as info:
        await make_organ(FakeCompletion(wasted, wasted, wasted, wasted), keys).process(ask())
    assert info.value.wasted_cost == pytest.approx(0.016)


@pytest.mark.parametrize("bad", [None, 12.0, True, -3])
@pytest.mark.asyncio
async def test_untrustworthy_usage_fields_are_estimated_not_recorded_as_zero(
    keys: KeyResolver, bad: Any
) -> None:
    # 审查 S2：usage 对象在、字段却不可信时，不能记成「已知的 0」。
    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="嗨"))],
        usage=SimpleNamespace(prompt_tokens=bad, completion_tokens=bad),
    )
    response = await make_organ(FakeCompletion(reply), keys).process(ask())
    assert response.metadata["usage_known"] is False
    assert response.metadata["cost"] is None
    [attempt] = response.attempts
    assert attempt.estimated is True
    assert attempt.prompt_tokens > 0
    assert attempt.completion_tokens > 0


@pytest.mark.asyncio
async def test_missing_usage_is_flagged_not_treated_as_free(keys: KeyResolver) -> None:
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="嗨"))])
    response = await make_organ(FakeCompletion(reply), keys).process(ask())
    assert response.metadata["usage_known"] is False
    assert response.metadata["cost"] is None  # 用量不明，费用也不明，不能记成 0
    # 往多里估（12.3 节）：输入用计数工具，输出按器官的 max_tokens（300）记满；只发了一次。
    [attempt] = response.attempts
    assert attempt.estimated is True
    assert attempt.prompt_tokens == COUNTED_PROMPT_TOKENS
    assert attempt.completion_tokens == 300


@pytest.mark.asyncio
async def test_system_context_is_sent_as_the_system_message(keys: KeyResolver) -> None:
    completion = FakeCompletion(make_response("嗨"))
    request = OrganRequest(organ_id="replyer", prompt="你好", system_context="  你是小绿。\n")

    await make_organ(completion, keys).process(request)

    assert completion.calls[0]["messages"][0]["content"] == "你是小绿。"


@pytest.mark.asyncio
async def test_without_system_context_a_neutral_system_message_is_sent(
    keys: KeyResolver,
) -> None:
    completion = FakeCompletion(make_response("嗨"))

    await make_organ(completion, keys).process(ask())

    assert completion.calls[0]["messages"][0]["content"] == EMPTY_SYSTEM_MESSAGE.default.template


def test_organ_without_a_chat_model_fails_at_startup(keys: KeyResolver) -> None:
    config = make_config(big_kind="embedding", organ_models=("big",))
    with pytest.raises(ConfigError, match="对话模型"):
        LLMOrgan.from_config("replyer", config, keys, FakeCompletion())


@pytest.mark.asyncio
async def test_an_empty_organ_makes_no_model_request(keys: KeyResolver) -> None:
    completion = FakeCompletion()
    organ = make_organ(completion, keys, organ_models=())
    with pytest.raises(OrganCallError) as error:
        await organ.process(ask())
    assert completion.calls == []
    assert error.value.attempts == []
    assert "未配置" in str(error.value)


def test_unknown_organ_fails_at_startup(keys: KeyResolver) -> None:
    with pytest.raises(ConfigError, match="没有器官 planner"):
        LLMOrgan.from_config("planner", make_config(), keys, FakeCompletion())


def make_unpriced_organ(
    completion: FakeCompletion,
    keys: KeyResolver,
    sleep: SleepRecorder | None = None,
    **organ_settings: Any,
) -> LLMOrgan:
    # 没写任何价格的模型（D37：价格可选）。默认重试 2 次，不写 max_tokens。
    config = ModelsConfig.model_validate(
        {
            "providers": [
                {"name": "cloud", "protocol": "openai", "api_key_env": "AUREUS_KEY_CLOUD"},
            ],
            "models": [{"name": "big", "identifier": "big-model", "provider": "cloud"}],
            "organs": {"replyer": {"models": ["big"], **organ_settings}},
        }
    )
    return LLMOrgan.from_config(
        "replyer", config, keys, completion, fixed_counter, sleep or SleepRecorder()
    )


@pytest.mark.asyncio
async def test_unpriced_model_reports_unknown_cost_but_still_counts_tokens(
    keys: KeyResolver,
) -> None:
    completion = FakeCompletion(make_response("嗨", prompt=1000, completion=100))
    response = await make_unpriced_organ(completion, keys).process(ask())

    assert response.suggestion == "嗨"
    assert response.metadata["cost"] is None  # 算不出来，不是 0
    assert response.metadata["prompt_tokens"] == 1000
    assert response.metadata["completion_tokens"] == 100
    assert response.metadata["wasted_cost"] == 0.0
    assert response.attempts[0].budget_tokens == 1100  # 没写价格也照样记 token


@pytest.mark.asyncio
async def test_unpriced_empty_reply_makes_the_wasted_cost_unknown(keys: KeyResolver) -> None:
    wasted = make_response(None, prompt=1000, completion=500)
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(FakeCompletion(wasted, wasted), keys).process(ask())
    assert info.value.wasted_cost is None


@pytest.mark.asyncio
async def test_rejected_request_records_zero_tokens_and_zero_cost_even_when_unpriced(
    keys: KeyResolver,
) -> None:
    # 服务商明确拒绝，没有处理请求：用量确实是 0，费用也确实是 0，不是“未知”（12.3 节）。
    completion = FakeCompletion(RateLimitError("x"))
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(completion, keys).process(ask())
    assert len(info.value.attempts) == 1  # 限流不重试（D132）
    for attempt in info.value.attempts:
        assert (attempt.prompt_tokens, attempt.completion_tokens) == (0, 0)
        assert attempt.estimated is False
        assert attempt.succeeded is False
        assert attempt.failure_reason == "被限流"
    assert info.value.wasted_cost == 0.0


@pytest.mark.asyncio
async def test_rejection_is_recognised_through_the_class_hierarchy(keys: KeyResolver) -> None:
    # litellm 的 ContextWindowExceededError 继承自 BadRequestError：也是明确拒绝。
    bad_request = type("BadRequestError", (Exception,), {})
    too_long = type("ContextWindowExceededError", (bad_request,), {})
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(FakeCompletion(too_long("x")), keys).process(ask())
    assert info.value.attempts[0].budget_tokens == 0
    assert info.value.attempts[0].failure_reason == "超出上下文长度"


@pytest.mark.asyncio
async def test_usage_records_both_attempts(keys: KeyResolver) -> None:
    # 超时不能当成免费：对方可能已经算完了（12.3 节）。D132 只重试一次，两次尝试各记一条。
    completion = FakeCompletion(*[Timeout("x")] * 2)
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(completion, keys).process(ask())
    assert len(completion.calls) == 2
    assert len(info.value.attempts) == 2
    for attempt in info.value.attempts:
        assert attempt.prompt_tokens == COUNTED_PROMPT_TOKENS
        assert attempt.completion_tokens == ASSUMED_COMPLETION_TOKENS
        assert attempt.estimated is True
        assert attempt.cost is None  # 用量是估算的，费用算不出来
    assert info.value.wasted_cost is None


@pytest.mark.asyncio
async def test_transient_failure_retries_once(keys: KeyResolver) -> None:
    # 超时重试一次（D132）：同一模型、同一请求，第二次成功。
    sleep = SleepRecorder()
    completion = FakeCompletion(Timeout("x"), make_response("嗨", prompt=10, completion=5))
    response = await make_unpriced_organ(completion, keys, sleep).process(ask())

    assert response.suggestion == "嗨"
    assert len(completion.calls) == 2
    assert [attempt.succeeded for attempt in response.attempts] == [False, True]
    assert sleep.delays == [RETRY_BACKOFF_SECONDS]

    # 直连路径的 5xx 同样重试一次。
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(500, json={"error": "x"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "嗨"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    direct = local_chat_organ(keys, httpx.MockTransport(respond))
    result = await direct.process(ask())
    assert result.suggestion == "嗨"
    assert len(requests) == 2
    assert [attempt.succeeded for attempt in result.attempts] == [False, True]
    await close_shared_clients()


@pytest.mark.parametrize(
    "error_name", ["RateLimitError", "AuthenticationError", "BadRequestError"]
)
@pytest.mark.asyncio
async def test_client_errors_do_not_retry(keys: KeyResolver, error_name: str) -> None:
    # 4xx / 认证失败 / 限流不重试（D132）。
    sleep = SleepRecorder()
    error_type = type(error_name, (Exception,), {})
    completion = FakeCompletion(error_type("x"))
    with pytest.raises(OrganCallError):
        await make_unpriced_organ(completion, keys, sleep).process(ask())
    assert len(completion.calls) == 1
    assert sleep.delays == []

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, json={"error": "x"})

    direct = local_chat_organ(keys, httpx.MockTransport(respond))
    with pytest.raises(OrganCallError, match="被限流"):
        await direct.process(ask())
    assert len(requests) == 1
    await close_shared_clients()


@pytest.mark.asyncio
async def test_retry_after_a_failure_records_both_requests(keys: KeyResolver) -> None:
    # 审查 H4：第一次超时、第二次成功，账本里要有两条，不能只剩成功的那条。
    sleep = SleepRecorder()
    completion = FakeCompletion(Timeout("x"), make_response("嗨", prompt=10, completion=5))
    response = await make_unpriced_organ(completion, keys, sleep).process(ask())

    assert response.suggestion == "嗨"
    assert [attempt.succeeded for attempt in response.attempts] == [False, True]
    assert response.attempts[0].estimated is True
    assert response.attempts[0].failure_reason == "超时"
    assert response.attempts[1].estimated is False
    assert response.attempts[1].budget_tokens == 15
    assert len(completion.calls) == 2
    assert sleep.delays == [RETRY_BACKOFF_SECONDS]


@pytest.mark.asyncio
async def test_retry_waits_before_the_second_attempt(keys: KeyResolver) -> None:
    sleep = SleepRecorder()
    completion = FakeCompletion(*[Timeout("x")] * 2)
    with pytest.raises(OrganCallError):
        await make_unpriced_organ(completion, keys, sleep).process(ask())
    assert sleep.delays == [RETRY_BACKOFF_SECONDS]  # 最后一次失败后不再等


@pytest.mark.parametrize("error_name", ["AuthenticationError", "BadRequestError"])
@pytest.mark.asyncio
async def test_errors_that_cannot_succeed_are_not_retried(
    keys: KeyResolver, error_name: str
) -> None:
    sleep = SleepRecorder()
    completion = FakeCompletion(type(error_name, (Exception,), {})("x"))
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(completion, keys, sleep).process(ask())
    assert len(completion.calls) == 1
    assert len(info.value.attempts) == 1
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_empty_reply_retries_once(keys: KeyResolver) -> None:
    # 空响应属于瞬时失败，重试一次（D132）；两次都空才报错。
    empty = make_response(None, prompt=10, completion=5)
    completion = FakeCompletion(empty, empty)
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(completion, keys).process(ask())
    assert len(completion.calls) == 2
    assert len(info.value.attempts) == 2
    assert all(attempt.failure_reason == EMPTY_REPLY_REASON for attempt in info.value.attempts)


@pytest.mark.asyncio
async def test_max_retry_zero_sends_exactly_one_request(tmp_path: Path) -> None:
    config = ModelsConfig.model_validate(
        {
            "providers": [{"name": "local", "protocol": "openai", "max_retry": 0}],
            "models": [{"name": "small", "identifier": "small-model", "provider": "local"}],
            "organs": {"replyer": {"models": ["small"]}},
        }
    )
    completion = FakeCompletion(Timeout("x"))
    organ = LLMOrgan.from_config(
        "replyer", config, KeyResolver(tmp_path / ".env", {}), completion, fixed_counter
    )
    with pytest.raises(OrganCallError) as info:
        await organ.process(ask())
    assert len(completion.calls) == 1
    assert len(info.value.attempts) == 1


@pytest.mark.asyncio
async def test_unknown_error_is_estimated_once_without_retrying(keys: KeyResolver) -> None:
    completion = FakeCompletion(ValueError("x"))
    organ = make_unpriced_organ(completion, keys, max_tokens=100)
    with pytest.raises(OrganCallError) as info:
        await organ.process(ask())
    assert len(completion.calls) == 1  # 不认识的错误不重试，免得白花钱
    assert info.value.attempts[0].completion_tokens == 100


@pytest.mark.asyncio
async def test_missing_key_reports_no_attempt(tmp_path: Path) -> None:
    # 根本没发出去的请求不进账本（12.2 节）。
    no_keys = KeyResolver(tmp_path / "absent.env", {})
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(FakeCompletion(), no_keys).process(ask())
    assert info.value.attempts == []
    assert info.value.wasted_cost == 0.0


@pytest.mark.asyncio
async def test_success_reports_every_attempt_in_order(keys: KeyResolver) -> None:
    wasted = make_response(None, prompt=1000, completion=500)
    completion = FakeCompletion(
        wasted, wasted, make_response("备用", prompt=10, completion=5)
    )
    response = await make_organ(completion, keys).process(ask())

    assert [(a.model, a.provider, a.succeeded) for a in response.attempts] == [
        ("big", "cloud", False),
        ("big", "cloud", False),
        ("small", "local", True),
    ]
    assert response.attempts[0].failure_reason == EMPTY_REPLY_REASON
    assert response.attempts[0].budget_tokens == 1500
    assert response.attempts[1].budget_tokens == 1500
    assert response.attempts[2].budget_tokens == 15


@pytest.mark.parametrize("bad_count", [RuntimeError("坏了"), 0, -5, True, "12"])
@pytest.mark.asyncio
async def test_broken_token_counter_falls_back_to_the_byte_length(
    keys: KeyResolver, bad_count: object
) -> None:
    def broken_counter(messages: list[dict[str, str]]) -> int:
        if isinstance(bad_count, Exception):
            raise bad_count
        return bad_count  # type: ignore[return-value]

    completion = FakeCompletion(*[Timeout("x")] * 3)
    organ = LLMOrgan.from_config(
        "replyer",
        make_config(organ_models=("small",)),
        keys,
        completion,
        broken_counter,
        SleepRecorder(),
    )
    with pytest.raises(OrganCallError) as info:
        await organ.process(ask())
    sent = completion.calls[0]["messages"]
    byte_length = sum(len(t.encode("utf-8")) for m in sent for t in m.values())
    assert info.value.attempts[0].prompt_tokens == byte_length


def test_real_token_counter_works_offline() -> None:
    # 真的 litellm 计数工具：只用随包带的分词表，不联网，返回正数。
    assert count_prompt_tokens([{"role": "user", "content": "你好，今天怎么样？"}]) > 0


@pytest.mark.parametrize("field_name", ["prompt_tokens", "completion_tokens", "cached_tokens"])
@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_call_attempt_rejects_bad_token_counts(field_name: str, bad: object) -> None:
    counts: dict[str, Any] = {"prompt_tokens": 1, "completion_tokens": 1, field_name: bad}
    with pytest.raises(ValueError, match=field_name):
        CallAttempt(model="m", provider="p", **counts)


@pytest.mark.asyncio
async def test_normal_call_falls_back_to_non_streaming(keys: KeyResolver) -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield make_stream_chunk(content=None)
        yield SimpleNamespace(choices=[], usage=make_response(None, 7, 1).usage)

    completion = FakeCompletion(chunks(), make_response("备用", prompt=10, completion=5))
    response = await make_unpriced_organ(completion, keys).process(
        OrganRequest("replyer", "规划材料", stream=True)
    )

    assert response.suggestion == "备用"
    assert len(completion.calls) == 2
    assert completion.calls[0]["stream"] is True
    assert completion.calls[0]["stream_options"] == {"include_usage": True}
    assert "stream" not in completion.calls[1]
    assert "stream_options" not in completion.calls[1]
    assert completion.calls[1]["model"] == completion.calls[0]["model"]
    assert completion.calls[1]["messages"] == completion.calls[0]["messages"]
    assert response.attempts[0].succeeded is False
    assert response.attempts[0].failure_reason == EMPTY_REPLY_REASON
    assert response.attempts[0].budget_tokens == 8
    assert response.attempts[1].succeeded is True
    assert response.attempts[1].budget_tokens == 15  # 用量以非流式响应为准
    assert response.metadata["prompt_tokens"] == 10
    assert response.metadata["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_fallback_keeps_tools_and_reads_tool_calls(keys: KeyResolver) -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield make_stream_chunk(content=None)

    tool_reply = make_response(None, 10, 5)
    tool_reply.choices[0].message.tool_calls = [
        make_tool_fragment('{"query": "资料"}', call_id="call_read", name="read")
    ]
    completion = FakeCompletion(chunks(), tool_reply)
    response = await make_planner(completion, keys).process(
        OrganRequest("planner", "规划材料", tools=(READ_TOOL,), stream=True)
    )

    assert response.suggestion == ""
    assert response.tool_calls == (ToolCall("read", "call_read", {"query": "资料"}),)
    assert len(completion.calls) == 2
    assert "stream" not in completion.calls[1]
    assert completion.calls[1]["tools"] == completion.calls[0]["tools"]
    assert completion.calls[1]["messages"] == completion.calls[0]["messages"]
    assert response.attempts[0].failure_reason == EMPTY_REPLY_REASON
    assert response.attempts[1].succeeded is True


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_reasoning_only_reply_is_not_sent_as_content(
    keys: KeyResolver, field: str, stream: bool
) -> None:
    def reasoning_reply() -> Any:
        reply = make_response(None, 100, 20)
        setattr(reply.choices[0].message, field, "内心推理")
        return reply

    outcomes: list[Any] = [reasoning_reply(), reasoning_reply()]
    if stream:

        async def chunks() -> AsyncIterator[Any]:
            delta = SimpleNamespace(content=None)
            setattr(delta, field, "内心推理")
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

        outcomes.insert(0, chunks())
    completion = FakeCompletion(*outcomes)
    with pytest.raises(OrganCallError, match=REASONING_ONLY_REASON) as error:
        await make_unpriced_organ(completion, keys).process(
            OrganRequest("replyer", "规划材料", stream=stream)
        )

    assert "内心推理" not in str(error.value)
    assert len(completion.calls) == 2
    if stream:
        assert completion.calls[0]["stream"] is True
        assert "stream" not in completion.calls[1]
    else:
        assert "stream" not in completion.calls[0]
        assert "stream" not in completion.calls[1]
    assert all(not attempt.succeeded for attempt in error.value.attempts)
    assert error.value.attempts[-1].failure_reason == REASONING_ONLY_REASON


@pytest.mark.asyncio
async def test_empty_after_fallback_keeps_empty_reason(keys: KeyResolver) -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield make_stream_chunk(content=None)

    completion = FakeCompletion(chunks(), make_response(None))
    with pytest.raises(OrganCallError, match=EMPTY_REPLY_REASON) as error:
        await make_unpriced_organ(completion, keys).process(
            OrganRequest("replyer", "规划材料", stream=True)
        )

    assert len(completion.calls) == 2
    assert "stream" not in completion.calls[1]
    assert [attempt.failure_reason for attempt in error.value.attempts] == [
        EMPTY_REPLY_REASON,
        EMPTY_REPLY_REASON,
    ]
    assert REASONING_ONLY_REASON not in str(error.value)


@pytest.mark.parametrize("error_name", ["Timeout", "RateLimitError", "AuthenticationError"])
@pytest.mark.parametrize("during_stream", [False, True])
@pytest.mark.asyncio
async def test_errors_do_not_trigger_fallback(
    keys: KeyResolver, error_name: str, during_stream: bool
) -> None:
    error_type = type(error_name, (Exception,), {})
    # 超时重试一次；限流和认证失败不重试（D132）。
    calls = 2 if error_name == "Timeout" else 1

    def outcome() -> Any:
        if not during_stream:
            return error_type("x")

        async def chunks() -> AsyncIterator[Any]:
            yield make_stream_chunk(content=None)
            raise error_type("x")

        return chunks()

    completion = FakeCompletion(*[outcome() for _ in range(calls)])
    with pytest.raises(OrganCallError) as error:
        await make_unpriced_organ(completion, keys).process(
            OrganRequest("replyer", "规划材料", stream=True)
        )

    assert len(completion.calls) == calls
    assert all(call.get("stream") is True for call in completion.calls)  # 没有非流式回退
    expected = {
        "Timeout": "超时",
        "RateLimitError": "被限流",
        "AuthenticationError": "认证失败（密钥无效？）",
    }[error_name]
    assert error.value.attempts[-1].failure_reason == expected


def test_litellm_console_handlers_are_removed_but_logs_still_propagate() -> None:
    import logging

    count_prompt_tokens([{"role": "user", "content": "你好"}])  # 触发导入

    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        litellm_logger = logging.getLogger(name)
        assert litellm_logger.handlers == []
        assert litellm_logger.propagate is True
