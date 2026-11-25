"""LLMOrgan（真实器官）的测试：全部用假的调用函数，不联网、不花钱。"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

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
    _MODEL_TEST_INPUT,
    ASSUMED_COMPLETION_TOKENS,
    EMPTY_REPLY_REASON,
    EMPTY_SYSTEM_MESSAGE,
    LITELLM_PREFIX_BY_PROTOCOL,
    REASONING_ONLY_REASON,
    RETRY_BACKOFF_SECONDS,
    EmbeddingOrgan,
    LLMOrgan,
    close_shared_clients,
    count_prompt_tokens,
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
            "messages": [{"role": "user", "content": _MODEL_TEST_INPUT}],
            "max_tokens": 32,
            "stream": True,
        }
    ]
    await close_shared_clients()


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
    assert call["timeout"] == 60.0
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


@pytest.mark.parametrize("protocol", list(LITELLM_PREFIX_BY_PROTOCOL))
@pytest.mark.asyncio
async def test_protocol_selects_the_litellm_model_prefix(keys: KeyResolver, protocol: str) -> None:
    completion = FakeCompletion(make_response("嗨"))
    await make_organ(completion, keys, protocol=protocol).process(ask())
    assert completion.calls[0]["model"] == LITELLM_PREFIX_BY_PROTOCOL[protocol] + "big-model"  # type: ignore[index]


@pytest.mark.asyncio
async def test_failed_model_falls_through_to_the_next(keys: KeyResolver) -> None:
    # 大模型重试用完（1 次加 2 次重试）仍被限流，才顺延到小模型。
    limited = [RateLimitError(f"限流 {CLOUD_SECRET}")] * 3
    completion = FakeCompletion(*limited, make_response("备用"))
    response = await make_organ(completion, keys).process(ask())

    assert response.suggestion == "备用"
    assert response.metadata["model"] == "small"
    assert response.metadata["failures_before_success"] == ["big：被限流"]
    # 本地服务商不需要密钥：不带 api_key，也不带云端的地址。
    assert "api_key" not in completion.calls[3]
    assert "api_base" not in completion.calls[3]


@pytest.mark.asyncio
async def test_all_models_failing_raises_without_leaking_the_secret(keys: KeyResolver) -> None:
    completion = FakeCompletion(*[RateLimitError(CLOUD_SECRET)] * 3, *[Timeout("x")] * 3)
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
    wasted = make_response("  ", prompt=1000, completion=500)
    completion = FakeCompletion(wasted, make_response("备用"))
    response = await make_organ(completion, keys).process(ask())

    assert response.metadata["wasted_cost"] == pytest.approx(0.008)
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
    completion = FakeCompletion(bad, bad)
    with pytest.raises(OrganCallError, match=EMPTY_REPLY_REASON):
        await make_organ(completion, keys).process(ask())


@pytest.mark.asyncio
async def test_wasted_cost_is_carried_by_the_final_error(keys: KeyResolver) -> None:
    wasted = make_response(None, prompt=1000, completion=500)
    with pytest.raises(OrganCallError) as info:
        await make_organ(FakeCompletion(wasted, wasted), keys).process(ask())
    assert info.value.wasted_cost == pytest.approx(0.008)


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
        await make_unpriced_organ(FakeCompletion(wasted), keys).process(ask())
    assert info.value.wasted_cost is None


@pytest.mark.asyncio
async def test_rejected_request_records_zero_tokens_and_zero_cost_even_when_unpriced(
    keys: KeyResolver,
) -> None:
    # 服务商明确拒绝，没有处理请求：用量确实是 0，费用也确实是 0，不是“未知”（12.3 节）。
    completion = FakeCompletion(*[RateLimitError("x")] * 3)
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(completion, keys).process(ask())
    assert len(info.value.attempts) == 3  # 限流值得重试：1 次加 2 次重试，每次都是一条记录
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
async def test_every_timed_out_request_is_estimated_as_used_and_recorded_separately(
    keys: KeyResolver,
) -> None:
    # 超时不能当成免费：对方可能已经算完了（12.3 节）。重试的每一次都是一条独立的记录。
    completion = FakeCompletion(*[Timeout("x")] * 3)
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(completion, keys).process(ask())
    assert len(completion.calls) == 3  # 1 次加上默认的 2 次重试
    assert len(info.value.attempts) == 3
    for attempt in info.value.attempts:
        assert attempt.prompt_tokens == COUNTED_PROMPT_TOKENS
        assert attempt.completion_tokens == ASSUMED_COMPLETION_TOKENS
        assert attempt.estimated is True
        assert attempt.cost is None  # 用量是估算的，费用算不出来
    assert info.value.wasted_cost is None


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
async def test_retry_waits_longer_each_time(keys: KeyResolver) -> None:
    sleep = SleepRecorder()
    completion = FakeCompletion(*[Timeout("x")] * 3)
    with pytest.raises(OrganCallError):
        await make_unpriced_organ(completion, keys, sleep).process(ask())
    assert sleep.delays == [
        RETRY_BACKOFF_SECONDS,
        RETRY_BACKOFF_SECONDS * 2,
    ]  # 最后一次失败后不再等


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
async def test_empty_reply_is_not_retried(keys: KeyResolver) -> None:
    # 空回复已经花了 token，再试一次只会再花一次。
    completion = FakeCompletion(make_response(None, prompt=10, completion=5))
    with pytest.raises(OrganCallError) as info:
        await make_unpriced_organ(completion, keys).process(ask())
    assert len(completion.calls) == 1
    assert len(info.value.attempts) == 1


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
    completion = FakeCompletion(wasted, make_response("备用", prompt=10, completion=5))
    response = await make_organ(completion, keys).process(ask())

    assert [(a.model, a.provider, a.succeeded) for a in response.attempts] == [
        ("big", "cloud", False),
        ("small", "local", True),
    ]
    assert response.attempts[0].failure_reason == EMPTY_REPLY_REASON
    assert response.attempts[0].budget_tokens == 1500
    assert response.attempts[1].budget_tokens == 15


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

    outcomes: list[Any] = [reasoning_reply()]
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
    assert len(completion.calls) == (2 if stream else 1)
    if stream:
        assert completion.calls[0]["stream"] is True
        assert "stream" not in completion.calls[1]
    else:
        assert "stream" not in completion.calls[0]
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
    calls = 3 if error_name in ("Timeout", "RateLimitError") else 1

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
