"""器官层：真实器官，通过 litellm 调用大模型（D24）。

回环地址的 OpenAI 兼容对话服务商直接走 HTTP `/chat/completions`，不经 litellm、
不走代理（D131）；其余服务商仍由 litellm 调用。

一个 LLMOrgan 对应 models.toml 里的一个器官（planner、replyer……），
按配置里的顺序尝试它的模型：前一个失败（密钥缺失、超时、限流、
返回为空……）才顺延到下一个，全部失败就抛 OrganCallError，
由调用方决定怎么降级。

器官只返回建议，不写任何状态：身体状态只读，被翻译成系统提示词里
的几句话；每一次真正发出去的请求（含失败的）都放进 attempts，交给安全网关记账（D39）。
"""

import asyncio
import atexit
import base64
import json
import os
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from ipaddress import ip_address
from math import isfinite
from types import SimpleNamespace
from typing import Any, Self

import httpx
from loguru import logger

from aureus.common.config import ConfigError
from aureus.common.prompt_text import PromptTemplate, render_prompt
from aureus.organs.api_keys import KeyResolver
from aureus.organs.interface import (
    LOCAL_EXPRESSION_EMBEDDING,
    CallAttempt,
    ModelPreference,
    OrganCallCancelled,
    OrganCallError,
    OrganRequest,
    OrganResponse,
    ToolCall,
    sum_costs,
)
from aureus.organs.llm_tools import ToolCallCollector, encode_tool_history, encode_tools
from aureus.organs.llm_usage import TokenUsage, compute_cost, extract_usage
from aureus.organs.model_config import (
    ModelConfig,
    ModelRoute,
    ModelsConfig,
    ProviderConfig,
    ProviderProtocol,
)
from aureus.organs.prompts import PromptStore

# 调用大模型的函数。默认是 litellm.acompletion；测试时换成假的，不联网。
CompletionFunction = Callable[..., Awaitable[Any]]
# 重试之间的等待函数。默认是 asyncio.sleep；测试时换成不等待的。
SleepFunction = Callable[[float], Awaitable[None]]
# 估算输入 token 数的函数。默认是 count_prompt_tokens；测试时换成假的，结果固定。
TokenCounter = Callable[[list[dict[str, str]]], int]


def _has_reasoning(value: Any) -> bool:
    """推理内容在 litellm 里可能叫 reasoning_content，也可能是 reasoning（D127）。"""
    for field in ("reasoning_content", "reasoning"):
        text = getattr(value, field, None)
        if isinstance(text, str) and text.strip():
            return True
    return False


class _FirstChunkTimeoutError(Exception):
    """测速的流式请求在首包超时前没有收到任何数据（D131）。"""


@dataclass
class _StreamProgress:
    """流式读取进度：是否收到过数据块，用来判断首包超时能否回退（D131）。"""

    received: bool = False


async def _read_stream(
    stream: Any,
    tools: ToolCallCollector | None = None,
    on_first_output: Callable[[], None] | None = None,
    *,
    first_chunk_timeout: float | None = None,
    progress: _StreamProgress | None = None,
) -> tuple[str | None, TokenUsage, bool]:
    """读取流式响应：正文、用量，以及有没有收到推理内容。

    Args:
        stream: litellm 的流式响应，或直连网关时由 `_direct_chat_stream` 生成的同形分片。
        tools: 收集工具调用的收集器；不需要时传 None。
        on_first_output: 第一次收到非空正文或推理内容时调用一次，测速用它记延迟。
        first_chunk_timeout: 等第一个数据块的最长秒数；超时抛 `_FirstChunkTimeoutError`（D131）。
        progress: 记录是否收到过任何数据块，测速用它判断首包超时能否回退。

    Returns:
        去掉首尾空白的正文（没有正文时为 None）、用量、是否收到过推理内容。
    """
    pieces: list[str] = []
    usage = TokenUsage(known=False)
    reasoning = False
    first_output = False
    first_chunk = True
    async with aclosing(stream):
        iterator = stream.__aiter__()
        while True:
            try:
                if first_chunk and first_chunk_timeout is not None:
                    async with asyncio.timeout(first_chunk_timeout):
                        chunk = await anext(iterator)
                else:
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            except TimeoutError as error:
                if first_chunk:
                    raise _FirstChunkTimeoutError() from error
                raise
            first_chunk = False
            if progress is not None:
                progress.received = True
            if chunk.choices:
                choice = chunk.choices[0]
                delta = choice.delta
                content = getattr(delta, "content", None)
                if isinstance(content, str):
                    pieces.append(content)
                visible = isinstance(content, str) and bool(content.strip())
                if _has_reasoning(delta):
                    reasoning = True
                    visible = True
                if visible and not first_output:
                    first_output = True
                    if on_first_output is not None:
                        on_first_output()
                if tools is not None:
                    tools.read_choice(choice, stream=True)
            actual = extract_usage(chunk)
            if actual.known:
                usage = actual
    return "".join(pieces).strip() or None, usage, reasoning


# 协议 → litellm 的模型前缀。openai/responses/ 触发 OpenAI Responses 协议；
# 四种写法都用 litellm 的 get_llm_provider 离线核对过。
LITELLM_PREFIX_BY_PROTOCOL: dict[ProviderProtocol, str] = {
    "openai": "openai/",
    "openai_responses": "openai/responses/",
    "anthropic": "anthropic/",
    "gemini": "gemini/",
}

# 失败原因的中文说明，按异常的类名匹配。只用类名、不用异常消息：
# 消息里可能回显请求内容或密钥（见 15 号文档第 4 节）。
FAILURE_REASON_BY_ERROR_NAME = {
    "Timeout": "超时",
    "TimeoutError": "超时",
    "TimeoutException": "超时",
    "RateLimitError": "被限流",
    "AuthenticationError": "认证失败（密钥无效？）",
    "ContextWindowExceededError": "超出上下文长度",
    "APIConnectionError": "连不上服务商",
    "ConnectError": "连不上服务商",
    "NetworkError": "连不上服务商",
    "ProtocolError": "连不上服务商",
    "ServiceUnavailableError": "服务商暂时不可用",
    "InternalServerError": "服务商内部错误",
    "BadRequestError": "请求被拒绝（参数不被支持？）",
}

EMPTY_REPLY_REASON = "返回内容为空或格式异常"
# 只收到推理内容、没有可见正文时的说明（D127）：测速算模型可用，普通回复按空回复处理。
REASONING_ONLY_REASON = "只返回了推理内容，没有可见正文"

# 服务商明确拒绝、没有处理这个请求的错误：这种失败记 0 token（06 号文档 12.3 节）。
# 其余错误（超时、连接中断、服务商内部错误、不认识的错误）可能已经用掉了 token，按估算记。
# 按类名匹配，沿继承链找：litellm 的 ContextWindowExceededError 继承自 BadRequestError，
# 也能匹配到。
REJECTED_BEFORE_PROCESSING_ERROR_NAMES = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "RateLimitError",
        "BadRequestError",
        "NotFoundError",
        "UnprocessableEntityError",
    }
)

# 值得重试的错误：临时性的（超时、断线、限流、服务商那边出问题），过一会可能就好了。
# 其余错误（认证失败、请求被拒、不认识的错误）重试也不会成功，只会多花钱，直接换下一个模型。
# 按类名匹配，沿继承链找，理由同上。
RETRYABLE_ERROR_NAMES = frozenset(
    {
        "Timeout",
        "TimeoutError",
        "RateLimitError",
        "APIConnectionError",
        "ServiceUnavailableError",
        "InternalServerError",
    }
)
# 重试前等待：第 n 次重试等 RETRY_BACKOFF_SECONDS * 2^(n-1) 秒，最多 MAX_BACKOFF_SECONDS 秒。
RETRY_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0

# 没配置 max_tokens、又没返回用量时，输出按这么多 token 记。
# 宁可多记（D37）：常见对话模型默认的最大输出长度，大多不超过它。
ASSUMED_COMPLETION_TOKENS = 8192


class _RouteFailureError(Exception):
    """某一条路线没能用。retryable 表示值得对同一个模型再试一次。"""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


def _failure_reason(error: Exception) -> str:
    # 直连路径自己判定的失败（比如返回体不是合法 JSON）已经带着中文说明。
    if isinstance(error, _RouteFailureError):
        return error.reason
    if isinstance(error, httpx.HTTPStatusError):
        return _http_status_reason(error.response.status_code)
    # 沿继承链找：litellm 的 Timeout 继承自 APIConnectionError，
    # 先匹配到更具体的那个。
    for error_class in type(error).__mro__:
        reason = FAILURE_REASON_BY_ERROR_NAME.get(error_class.__name__)
        if reason is not None:
            return reason
    return f"未知错误（{type(error).__name__}）"


def _http_status_reason(status: int) -> str:
    """把直连请求的 HTTP 状态码翻译成和 litellm 一致的失败说明。"""
    if status in (401, 403):
        return "认证失败（密钥无效？）"
    if status == 429:
        return "被限流"
    if status in (400, 404, 422):
        return "请求被拒绝（参数不被支持？）"
    if status >= 500:
        return "服务商内部错误"
    return f"服务商返回 HTTP {status}"


def _rejected_before_processing(error: Exception) -> bool:
    return any(
        error_class.__name__ in REJECTED_BEFORE_PROCESSING_ERROR_NAMES
        for error_class in type(error).__mro__
    )


def _provider_arguments(provider: ProviderConfig, resolver: KeyResolver) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"timeout": provider.timeout_seconds, "num_retries": 0}
    if provider.base_url is not None:
        kwargs["api_base"] = provider.base_url
    if provider.api_key_ref is not None or provider.api_key_env is not None:
        key = (
            resolver.get_vault(provider.api_key_ref)
            if provider.api_key_ref is not None
            else resolver.get(provider.api_key_env or "")
        )
        if key is None:
            raise _RouteFailureError(
                "引用的 API key 不存在或类型不匹配"
                if provider.api_key_ref is not None
                else f"密钥缺失（{provider.api_key_env} 没有填写）"
            )
        kwargs["api_key"] = key.get_secret_value()
    return kwargs


def _retryable(error: Exception) -> bool:
    return any(error_class.__name__ in RETRYABLE_ERROR_NAMES for error_class in type(error).__mro__)


def _byte_length(messages: list[dict[str, str]]) -> int:
    # 计数工具出错时的退路：一个 token 至少对应一个字节，按字节数记只会多不会少。
    return sum(len(text.encode("utf-8")) for message in messages for text in message.values())


def _import_litellm() -> Any:
    # 费用按 models.toml 的价格自己算，用不到 litellm 自带的价格表；它默认
    # 启动时联网下载，这里改用随包带的那份，免得启动被网络卡住。
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    # 放在函数里导入：litellm 导入很慢，只有真正用到时才付这个代价。
    import logging

    import litellm

    # 不让 litellm 去 HuggingFace 下载分词器：会联网，还会把缓存写到用户目录、
    # 落在数据根目录之外（D38）。随包带的 tiktoken 分词表就够估算用了。
    litellm.disable_hf_tokenizer_download = True
    # litellm 给自己的日志器挂了往终端输出的处理器；日志已经统一转给 loguru
    # （过密钥过滤、进日志文件），留着它们，终端里每条会打两遍。只摘处理器，日志照样向上传。
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        logging.getLogger(name).handlers.clear()
    return litellm


def count_prompt_tokens(messages: list[dict[str, str]]) -> int:
    """用 litellm 的计数工具估算输入的 token 数（06 号文档 12.3 节）。

    只用随包带的 tiktoken 分词表，不联网。不同模型的分词方式不同，这里只是估算。

    Args:
        messages: 发给模型的消息。

    Returns:
        估算的 token 数。
    """
    return _import_litellm().token_counter(model="", messages=messages)


def _read_text(response: Any) -> str | None:
    try:
        text = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return None
    return text.strip() if isinstance(text, str) and text.strip() else None


def _read_reasoning(response: Any) -> bool:
    """非流式响应里有没有推理内容；只用来判断可用性和写失败说明，不当正文（D127）。"""
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return False
    return _has_reasoning(message)


def _load_litellm_completion() -> CompletionFunction:
    # 创建真实器官发生在启动阶段，不在心跳循环里，导入慢一点不要紧。
    return _import_litellm().acompletion


_MODEL_TEST_INPUT = "Reply OK."
_MODEL_TEST_MAX_TOKENS = 32
# 测速的流式请求最多等这么久要第一个数据块（D131）：本地网关可能既不返回数据也不结束。
PROBE_FIRST_CHUNK_TIMEOUT_SECONDS = 20.0

# 固定的红色 PNG，测速不需要读取用户图片。
_PROBE_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def model_probe_inputs(model: ModelConfig) -> dict[str, dict[str, Any]]:
    """列出已声明的每项能力及其实际测试输入。"""
    if model.kind == "embedding":
        return {"embedding": {"input": [_MODEL_TEST_INPUT]}}
    if model.kind == "image_embedding":
        return {"image_embedding": {"input": [{"image": _PROBE_IMAGE.split(",", 1)[1]}]}}
    if model.kind == "transcription":
        return {"transcription": {"file": "一秒静音 WAV"}}
    message = {"role": "user", "content": _MODEL_TEST_INPUT}
    probes: dict[str, dict[str, Any]] = {"text": {"messages": [message]}}
    if model.vision:
        probes["vision"] = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Name the color of this image in one English word.",
                        },
                        {"type": "image_url", "image_url": {"url": _PROBE_IMAGE}},
                    ],
                }
            ]
        }
    probes["tools"] = {
        "messages": [{"role": "user", "content": "Call echo with value OK."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }
        ],
    }
    for level in model.reasoning_levels:
        probes["reasoning/" + level] = {"messages": [message], "reasoning_effort": level}
    return probes


def _probe_usage(inputs: dict[str, Any], completion_tokens: int) -> TokenUsage:
    """测速拿不到可信用量时，按一次请求往多里估（06 号文档 12.3 节）。"""
    return TokenUsage(
        prompt_tokens=len(json.dumps(inputs).encode()),
        completion_tokens=completion_tokens,
        known=False,
    )


def _probe_attempt(
    model: ModelConfig, provider: ProviderConfig, usage: TokenUsage, failure: str | None
) -> CallAttempt:
    return CallAttempt(
        model=model.name,
        provider=provider.name,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_tokens=usage.cached_tokens,
        estimated=not usage.known,
        succeeded=failure is None,
        failure_reason=failure,
        cost=0.0
        if usage.known and not (usage.prompt_tokens or usage.completion_tokens)
        else compute_cost(model, usage),
    )


@dataclass
class _SharedClientEntry:
    """同一个事件循环里共用的一条 HTTP 客户端（D128、D131）。"""

    loop: asyncio.AbstractEventLoop
    transport: httpx.AsyncBaseTransport | None
    trust_env: bool
    client: httpx.AsyncClient


_SHARED_CLIENTS: list[_SharedClientEntry] = []


def _shared_client(
    transport: httpx.AsyncBaseTransport | None, *, trust_env: bool
) -> httpx.AsyncClient:
    """取得当前事件循环共用的 HTTP 客户端，保留连接池（D128、D131）。

    客户端绑定事件循环，不能跨循环共用，所以按循环分开；transport 只用于测试。
    """
    loop = asyncio.get_running_loop()
    for entry in _SHARED_CLIENTS:
        if (
            entry.loop is loop
            and entry.transport is transport
            and entry.trust_env is trust_env
            and not entry.client.is_closed
        ):
            return entry.client
    client = httpx.AsyncClient(transport=transport, trust_env=trust_env)
    _SHARED_CLIENTS.append(_SharedClientEntry(loop, transport, trust_env, client))
    return client


async def close_shared_clients() -> None:
    """关闭当前事件循环里共用的 HTTP 客户端（D128、D131）。"""
    loop = asyncio.get_running_loop()
    for entry in list(_SHARED_CLIENTS):
        if entry.loop is loop:
            _SHARED_CLIENTS.remove(entry)
            await entry.client.aclose()


def _close_shared_clients_at_exit() -> None:
    """进程退出时关闭所有还开着的共享客户端（D128）。

    正式运行的 asyncio.run 结束时，事件循环已经关闭，不能再在里面等待；
    这里用一个短循环关闭连接池。仍在别的线程运行的循环交给它自己收尾。
    """
    entries = list(_SHARED_CLIENTS)
    _SHARED_CLIENTS.clear()
    for entry in entries:
        if entry.client.is_closed or entry.loop.is_running():
            continue
        try:
            asyncio.run(entry.client.aclose())
        except Exception as error:
            logger.debug("退出时关闭共享客户端失败：{}", type(error).__name__)


atexit.register(_close_shared_clients_at_exit)


def _request_time() -> float:
    """直连请求的计时点；测试可以换成假时钟（D128）。"""
    return asyncio.get_running_loop().time()


async def _post_embeddings(
    base: str,
    payload: dict[str, Any],
    params: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    local: bool = False,
    on_request: Callable[[float], None] | None = None,
) -> Any:
    """直接请求 OpenAI 兼容的 `/embeddings`，形状与 litellm 的嵌入响应一致（D128）。

    Args:
        base: 服务商地址，末尾不带斜杠。
        payload: 请求体。
        params: 服务商参数，提供超时和可选的 API 密钥。
        transport: 测试用的假网络；正式运行传 None。
        local: 回环免费端点，不使用环境里的代理。
        on_request: 收到完整响应后回调这次请求的耗时（秒），测速用。

    Returns:
        带 data 和 usage 的响应；data 由调用方校验。
    """
    headers = {"Authorization": "Bearer " + params["api_key"]} if "api_key" in params else {}
    client = _shared_client(transport, trust_env=not local)
    started = _request_time() if on_request is not None else 0.0
    response = await client.post(
        base + "/embeddings", headers=headers, json=payload, timeout=params["timeout"]
    )
    if on_request is not None:
        on_request(_request_time() - started)
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as error:
        raise _RouteFailureError(EMPTY_REPLY_REASON) from error
    if not isinstance(result, dict):
        raise _RouteFailureError(EMPTY_REPLY_REASON)
    usage = result.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return SimpleNamespace(
        data=result.get("data"),
        usage=SimpleNamespace(
            prompt_tokens=usage.get("prompt_tokens", usage.get("total_tokens")),
            completion_tokens=0,
        ),
    )


def _tool_call_object(raw: Any) -> Any:
    """把 OpenAI 的 tool_calls 转成 litellm 那样的属性形状（D131）。"""
    if not isinstance(raw, dict):
        return raw
    function = raw.get("function")
    function_object = SimpleNamespace(name=None, arguments=None)
    if isinstance(function, dict):
        function_object = SimpleNamespace(
            name=function.get("name"), arguments=function.get("arguments")
        )
    fields = {key: raw[key] for key in ("id", "index", "type") if key in raw}
    return SimpleNamespace(**fields, function=function_object)


def _usage_object(raw: Any) -> SimpleNamespace | None:
    """把 OpenAI 的 usage 转成 `extract_usage` 认识的形状；缺字段保持 None。"""
    if not isinstance(raw, dict):
        return None
    details = raw.get("prompt_tokens_details")
    return SimpleNamespace(
        prompt_tokens=raw.get("prompt_tokens"),
        completion_tokens=raw.get("completion_tokens"),
        prompt_tokens_details=(
            SimpleNamespace(cached_tokens=details.get("cached_tokens"))
            if isinstance(details, dict)
            else None
        ),
    )


def _chat_message(message: dict[str, Any]) -> SimpleNamespace:
    """把 OpenAI 的 message 或 delta 转成 litellm 那样的对象（D131）。"""
    fields: dict[str, Any] = {"content": message.get("content")}
    for name in ("reasoning_content", "reasoning"):
        if name in message:
            fields[name] = message[name]
    calls = message.get("tool_calls")
    if calls is not None:
        fields["tool_calls"] = (
            [_tool_call_object(call) for call in calls] if isinstance(calls, list) else calls
        )
    return SimpleNamespace(**fields)


def _chat_response(result: dict[str, Any]) -> Any:
    """把非流式 `/chat/completions` 返回体转成 litellm 的响应形状（D131）。"""
    choices: list[SimpleNamespace] = []
    raw_choices = result.get("choices")
    if isinstance(raw_choices, list):
        for raw in raw_choices:
            if not isinstance(raw, dict):
                continue
            message = raw.get("message")
            choices.append(
                SimpleNamespace(
                    message=_chat_message(message if isinstance(message, dict) else {})
                )
            )
    response = SimpleNamespace(choices=choices)
    usage = _usage_object(result.get("usage"))
    if usage is not None:
        response.usage = usage
    return response


def _stream_chunk(result: dict[str, Any]) -> Any:
    """把一条流式 `/chat/completions` 数据转成 litellm 的流式分片形状（D131）。"""
    choices: list[SimpleNamespace] = []
    raw_choices = result.get("choices")
    if isinstance(raw_choices, list):
        for raw in raw_choices:
            if not isinstance(raw, dict):
                continue
            delta = raw.get("delta")
            choices.append(
                SimpleNamespace(
                    delta=_chat_message(delta if isinstance(delta, dict) else {}),
                    finish_reason=raw.get("finish_reason"),
                )
            )
    chunk = SimpleNamespace(choices=choices)
    usage = _usage_object(result.get("usage"))
    if usage is not None:
        chunk.usage = usage
    return chunk


def _chat_headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": "Bearer " + api_key} if api_key else {}


async def _post_chat(
    base: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    on_request: Callable[[float], None] | None = None,
) -> Any:
    """直接请求回环 OpenAI 兼容网关的 `/chat/completions`（D131）。

    Args:
        base: 服务商地址，末尾不带斜杠。
        payload: 请求体。
        timeout: 服务商配置的超时（秒）。
        api_key: 有密钥时放进 Authorization 头。
        transport: 测试用的假网络；正式运行传 None。
        on_request: 收到完整响应后回调这次请求的耗时（秒），测速用。

    Returns:
        与 litellm 非流式响应形状一致的响应。
    """
    client = _shared_client(transport, trust_env=False)
    started = _request_time() if on_request is not None else 0.0
    response = await client.post(
        base + "/chat/completions",
        headers=_chat_headers(api_key),
        json=payload,
        timeout=timeout,
    )
    if on_request is not None:
        on_request(_request_time() - started)
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as error:
        raise _RouteFailureError(EMPTY_REPLY_REASON) from error
    if not isinstance(result, dict):
        raise _RouteFailureError(EMPTY_REPLY_REASON)
    return _chat_response(result)


async def _direct_chat_stream(
    base: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[Any]:
    """直接读取回环 OpenAI 兼容网关的流式 `/chat/completions`（D131）。"""
    client = _shared_client(transport, trust_env=False)
    async with client.stream(
        "POST",
        base + "/chat/completions",
        headers=_chat_headers(api_key),
        json=payload,
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                result = json.loads(data)
            except ValueError as error:
                raise _RouteFailureError(EMPTY_REPLY_REASON) from error
            if not isinstance(result, dict):
                raise _RouteFailureError(EMPTY_REPLY_REASON)
            yield _stream_chunk(result)


async def _send_chat(
    route: ModelRoute,
    completion: CompletionFunction | None,
    kwargs: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    on_request: Callable[[float], None] | None = None,
) -> Any:
    """按路线发出一次对话请求：回环 OpenAI 兼容服务商直连，其余走 litellm（D131）。

    Args:
        route: 这次走的模型路线。
        completion: litellm 的调用函数；本地直连时用不到，可以是 None。
        kwargs: litellm 风格的请求参数（模型、消息、超时等）。
        transport: 测试用的假网络；正式运行传 None。
        on_request: 直连非流式请求的耗时回调，测速用。

    Returns:
        非流式响应，或可直接读取的流式响应。
    """
    endpoint = local_chat_endpoint(route)
    if endpoint is None:
        assert completion is not None  # 远程路线在构造器官时已加载 litellm。
        return await completion(**kwargs)
    payload = {"model": route.model.identifier}
    for key in (
        "messages",
        "temperature",
        "max_tokens",
        "reasoning_effort",
        "tools",
        "stream",
        "stream_options",
    ):
        if key in kwargs:
            payload[key] = kwargs[key]
    timeout = kwargs["timeout"]
    api_key = kwargs.get("api_key")
    if payload.get("stream"):
        return _direct_chat_stream(
            endpoint, payload, timeout, api_key=api_key, transport=transport
        )
    return await _post_chat(
        endpoint, payload, timeout, api_key=api_key, transport=transport, on_request=on_request
    )


async def probe_model(
    model: ModelConfig,
    provider: ProviderConfig,
    resolver: KeyResolver,
    capability: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OrganResponse:
    """测量首段正文（或推理内容）或向量的返回时间，报告一次调用的用量。

    Args:
        model: 要测的模型。
        provider: 模型所属的服务商。
        resolver: 密钥解析器。
        capability: 要测的能力；不给就按模型类型推断。
        transport: 测试用的假网络；正式运行传 None。
    """
    organ_id = "model_test"
    if model.kind == "image_embedding":
        organ = EmbeddingOrgan(
            organ_id,
            [ModelRoute(model, provider, None, None)],
            resolver,
            transport=transport,
        )
        image = base64.b64decode(_PROBE_IMAGE.split(",", 1)[1])
        measured: list[float] = []
        response = await organ.process(
            OrganRequest(organ_id, "", embedding_inputs=(image,)),
            on_request=measured.append,
        )
        response.suggestion = json.dumps(response.embeddings[0])
        response.metadata.update(latency_ms=round(measured[-1] * 1000), kind=model.kind)
        return response
    route = ModelRoute(model, provider, None, None)
    if model.kind == "embedding" and local_expression_endpoint(route) is not None:
        # 回环免费的嵌入直连，不导入也不经过 litellm（D128）。
        organ = EmbeddingOrgan(organ_id, [route], resolver, transport=transport)
        measured = []
        response = await organ.process(
            OrganRequest(organ_id, "", embedding_inputs=(_MODEL_TEST_INPUT,)),
            on_request=measured.append,
        )
        response.suggestion = json.dumps(response.embeddings[0])
        response.metadata.update(latency_ms=round(measured[-1] * 1000), kind=model.kind)
        return response
    try:
        params = _provider_arguments(provider, resolver)
    except _RouteFailureError as error:
        raise OrganCallError(organ_id, [error.reason]) from None
    embedding = model.kind == "embedding"
    transcription = model.kind == "transcription"
    capability = capability or (model.kind if embedding or transcription else "text")
    inputs = model_probe_inputs(model)[capability]
    reply = ""
    protocol = provider.protocol
    if transcription:
        if protocol not in ("openai", "openai_responses"):
            raise OrganCallError(organ_id, ["语音转写需要兼容 OpenAI 音频转写的服务商。"])
        protocol = "openai"
    if embedding:
        if protocol == "anthropic":
            raise OrganCallError(organ_id, ["Anthropic 协议没有嵌入接口。"])
        if protocol == "openai_responses":
            protocol = "openai"
    endpoint = None if embedding or transcription else local_chat_endpoint(route)
    # 回环 OpenAI 兼容对话直连，不导入 litellm（D131）；其余能力照旧走 litellm。
    library = None if endpoint is not None else await asyncio.to_thread(_import_litellm)
    completion: CompletionFunction | None = None
    if not embedding and not transcription and library is not None:
        completion = library.acompletion
    if endpoint is not None:
        # 先把共享客户端建好，测速延迟只从真正发出请求算起（D131）。
        _shared_client(transport, trust_env=False)
    params["model"] = LITELLM_PREFIX_BY_PROTOCOL[protocol] + model.identifier
    loop = asyncio.get_running_loop()
    started = loop.time()
    latency = None
    failure = None
    attempts: list[CallAttempt] = []

    def mark_latency() -> None:
        nonlocal latency
        if latency is None:
            latency = loop.time() - started

    usage = _probe_usage(
        inputs, 0 if embedding or transcription else _MODEL_TEST_MAX_TOKENS
    )
    try:
        if transcription:
            assert library is not None
            async with asyncio.timeout(provider.timeout_seconds):
                result = await library.atranscription(file=_probe_audio(), **params)
            if isinstance(result.text, str):
                latency = loop.time() - started
                reply = result.text.strip() or "接口已返回，静音没有可转写的文字。"
            actual = extract_usage(result)
            if actual.known:
                usage = actual
        elif embedding:
            assert library is not None
            async with asyncio.timeout(provider.timeout_seconds):
                result = await library.aembedding(**inputs, **params)
            if result.data and result.data[0].get("embedding"):
                latency = loop.time() - started
                reply = json.dumps(result.data[0]["embedding"])
            actual = extract_usage(result)
            if actual.known:
                usage = actual
        elif capability == "tools":
            measured: list[float] = []
            async with asyncio.timeout(provider.timeout_seconds):
                result = await _send_chat(
                    route,
                    completion,
                    {**params, **inputs, "max_tokens": _MODEL_TEST_MAX_TOKENS},
                    transport=transport,
                    on_request=measured.append,
                )
            latency = measured[-1] if measured else loop.time() - started
            actual = extract_usage(result)
            if actual.known:
                usage = actual
            message = result.choices[0].message
            calls = getattr(message, "tool_calls", None) or []
            reply = (
                json.dumps(
                    [
                        {"name": call.function.name, "arguments": call.function.arguments}
                        for call in calls
                    ],
                    ensure_ascii=False,
                )
                if calls
                else (message.content or "")
            )
            if (
                len(calls) != 1
                or calls[0].function.name != "echo"
                or json.loads(calls[0].function.arguments) != {"value": "OK"}
            ):
                failure = "工具调用名称或参数与测试要求不符。"
        else:
            first_timeout = min(provider.timeout_seconds, PROBE_FIRST_CHUNK_TIMEOUT_SECONDS)
            progress = _StreamProgress()
            first_chunk_timed_out = False
            text: str | None = None
            reasoning = False
            try:
                async with asyncio.timeout(provider.timeout_seconds):
                    stream = await _send_chat(
                        route,
                        completion,
                        {
                            **params,
                            **inputs,
                            "stream": True,
                            "max_tokens": _MODEL_TEST_MAX_TOKENS,
                        },
                        transport=transport,
                    )
                    text, usage, reasoning = await _read_stream(
                        stream,
                        on_first_output=mark_latency,
                        first_chunk_timeout=first_timeout,
                        progress=progress,
                    )
            except _FirstChunkTimeoutError:
                first_chunk_timed_out = True
            except TimeoutError:
                if progress.received:
                    raise
                # 服务商超时不超过 20 秒时首包超时与总超时同时到期，同样按首包超时处理。
                first_chunk_timed_out = True
            if first_chunk_timed_out:
                # 首包超时且没有任何数据：取消这次流式请求，按非流式重试一次（D131）。
                attempts.append(
                    _probe_attempt(
                        model,
                        provider,
                        _probe_usage(inputs, _MODEL_TEST_MAX_TOKENS),
                        "超时",
                    )
                )
                usage = _probe_usage(inputs, _MODEL_TEST_MAX_TOKENS)
                async with asyncio.timeout(provider.timeout_seconds):
                    result = await _send_chat(
                        route,
                        completion,
                        {**params, **inputs, "max_tokens": _MODEL_TEST_MAX_TOKENS},
                        transport=transport,
                    )
                actual = extract_usage(result)
                if actual.known:
                    usage = actual
                text = _read_text(result)
                reasoning = _read_reasoning(result)
                if text is not None:
                    reply = text
                    latency = loop.time() - started
                elif reasoning:
                    reply = REASONING_ONLY_REASON
                    latency = loop.time() - started
            else:
                reply = text or ""
                if text is None:
                    # 流式没有正文：同一请求去掉 stream 再试一次（D127）。这次重试先按
                    # 一次请求估算，拿到非流式响应的真实用量再覆盖（用量以非流式为准）。
                    if not usage.known:
                        usage = _probe_usage(inputs, _MODEL_TEST_MAX_TOKENS)
                    attempts.append(
                        _probe_attempt(
                            model,
                            provider,
                            usage,
                            REASONING_ONLY_REASON if reasoning else EMPTY_REPLY_REASON,
                        )
                    )
                    usage = _probe_usage(inputs, _MODEL_TEST_MAX_TOKENS)
                    async with asyncio.timeout(provider.timeout_seconds):
                        result = await _send_chat(
                            route,
                            completion,
                            {**params, **inputs, "max_tokens": _MODEL_TEST_MAX_TOKENS},
                            transport=transport,
                        )
                    actual = extract_usage(result)
                    if actual.known:
                        usage = actual
                    text = _read_text(result)
                    reasoning = reasoning or _read_reasoning(result)
                    if text is not None:
                        reply = text
                        latency = loop.time() - started
                    elif reasoning:
                        reply = REASONING_ONLY_REASON
                        latency = loop.time() - started
    except Exception as error:
        failure = _failure_reason(error)
        if _rejected_before_processing(error):
            usage = TokenUsage()
    if latency is None and failure is None:
        failure = EMPTY_REPLY_REASON
    if (
        capability == "vision"
        and latency is not None
        and reply.strip().rstrip(".!").casefold() != "red"
    ):
        failure = failure or "未识别出测试图片的红色。"
    attempts.append(_probe_attempt(model, provider, usage, failure))
    if failure is not None:
        raise ModelProbeError(failure, attempts, reply)
    assert latency is not None
    return OrganResponse(
        organ_id,
        reply,
        metadata={"latency_ms": round(latency * 1000), "kind": model.kind},
        attempts=attempts,
    )


class ModelProbeError(OrganCallError):
    """保留已收到的测试回复，网关仍按失败尝试记账。"""

    def __init__(self, reason: str, attempts: Sequence[CallAttempt], reply: str) -> None:
        super().__init__("model_test", [reason], list(attempts))
        self.reply = reply


class EmbeddingOrgan:
    """计算文本或图片向量，并报告每次请求的用量。"""

    def __init__(
        self,
        organ_id: str,
        routes: Sequence[ModelRoute],
        resolver: KeyResolver,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.organ_id = organ_id
        self._routes = routes
        self._resolver = resolver
        self._transport = transport

    @property
    def local_expression_space(self) -> str | None:
        """发送前就能确定的表达空间；仅接受单一免费回环路线。"""
        if self.organ_id != LOCAL_EXPRESSION_EMBEDDING or len(self._routes) != 1:
            return None
        route = self._routes[0]
        endpoint = local_expression_endpoint(route)
        if endpoint is None:
            return None
        identity = (route.provider.name, endpoint, route.model.name, route.model.identifier)
        return sha256(json.dumps(identity).encode()).hexdigest()

    def spaces(self) -> tuple[tuple[int, str], ...]:
        """返回已接线的路线序号和实际空间键，不读密钥或探测服务。"""
        if self.organ_id == LOCAL_EXPRESSION_EMBEDDING:
            space = self.local_expression_space
            return ((1, space),) if space else ()
        return tuple(
            (number, self._route_space(route))
            for number, route in enumerate(self._routes, 1)
            if route.model.kind == "image_embedding" or route.provider.protocol != "anthropic"
        )

    @staticmethod
    def _route_space(route: ModelRoute) -> str:
        identity = (route.provider.name, route.provider.base_url, route.model.identifier)
        return sha256(json.dumps(identity).encode()).hexdigest()

    async def process(
        self,
        request: OrganRequest,
        *,
        on_request: Callable[[float], None] | None = None,
    ) -> OrganResponse:
        """按器官路线计算一批向量，保持输入顺序。

        Args:
            request: 嵌入材料。
            on_request: 直连请求收到响应后回调耗时（秒），测速用；业务调用不传。
        """
        inputs = request.embedding_inputs
        if not inputs:
            raise OrganCallError(self.organ_id, ["没有嵌入材料"])
        local_space = self.local_expression_space
        if self.organ_id == LOCAL_EXPRESSION_EMBEDDING and (
            local_space is None or request.embedding_space != local_space
        ):
            raise OrganCallError(self.organ_id, ["本地表达嵌入路线已变更或不可用"])
        attempts: list[CallAttempt] = []
        failures: list[str] = []
        for route in order_routes(self._routes, request.model_preference):
            try:
                params = _provider_arguments(route.provider, self._resolver)
                if route.model.kind == "image_embedding":
                    if not all(isinstance(value, bytes) for value in inputs):
                        raise _RouteFailureError("图片嵌入需要图片字节")
                elif route.provider.protocol == "anthropic" or not all(
                    isinstance(value, str) for value in inputs
                ):
                    raise _RouteFailureError("路线不支持文本嵌入输入")
            except _RouteFailureError as error:
                failures.append(f"{route.model.name}：{error.reason}")
                continue
            usage = TokenUsage(
                prompt_tokens=sum(
                    len(value.encode("utf-8")) if isinstance(value, str) else len(value)
                    for value in inputs
                ),
                known=False,
            )
            failure = None
            vectors: tuple[tuple[float, ...], ...] = ()
            try:
                async with asyncio.timeout(route.provider.timeout_seconds):
                    result = await self._send(route, inputs, params, on_request=on_request)
                actual = extract_usage(result)
                if actual.known:
                    usage = actual
                vectors = _read_embedding_batch(result.data, len(inputs))
                if not vectors:
                    failure = EMPTY_REPLY_REASON
            except Exception as error:
                failure = _failure_reason(error)
                if _rejected_before_processing(error) or (
                    isinstance(error, httpx.HTTPStatusError)
                    and error.response.status_code in (400, 401, 403, 404, 422, 429)
                ):
                    usage = TokenUsage()
            attempts.append(
                CallAttempt(
                    route.model.name,
                    route.provider.name,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    cached_tokens=usage.cached_tokens,
                    estimated=not usage.known,
                    succeeded=failure is None,
                    failure_reason=failure,
                    cost=0.0
                    if local_space or usage == TokenUsage()
                    else compute_cost(route.model, usage),
                )
            )
            if failure is None:
                space = local_space or self._route_space(route)
                return OrganResponse(
                    self.organ_id,
                    "",
                    metadata={"embedding_space": space},
                    attempts=attempts,
                    embeddings=vectors,
                )
            failures.append(f"{route.model.name}：{failure}")
        raise OrganCallError(self.organ_id, failures or ["未配置可用的模型"], attempts)

    async def _send(
        self,
        route: ModelRoute,
        inputs: tuple[str | bytes, ...],
        params: dict[str, Any],
        *,
        on_request: Callable[[float], None] | None = None,
    ) -> Any:
        if route.model.kind == "embedding":
            base = local_expression_endpoint(route)
            if base is not None:
                # 回环免费的嵌入直连，复用连接池（D128）。
                return await _post_embeddings(
                    base,
                    {"model": route.model.identifier, "input": list(inputs)},
                    params,
                    transport=self._transport,
                    local=True,
                    on_request=on_request,
                )
            if self.organ_id == LOCAL_EXPRESSION_EMBEDDING:
                raise _RouteFailureError("表达嵌入需要本机回环地址")
            protocol = route.provider.protocol
            if protocol == "openai_responses":
                protocol = "openai"
            library = await asyncio.to_thread(_import_litellm)
            return await library.aembedding(
                model=LITELLM_PREFIX_BY_PROTOCOL[protocol] + route.model.identifier,
                input=list(inputs),
                **params,
            )
        base = (route.provider.base_url or "https://api.jina.ai/v1").rstrip("/")
        payload = {
            "model": route.model.identifier,
            "embedding_type": "float",
            "input": [
                {"image": base64.b64encode(value).decode("ascii")}
                for value in inputs
                if isinstance(value, bytes)
            ],
        }
        return await _post_embeddings(
            base, payload, params, transport=self._transport, on_request=on_request
        )


def _loopback_endpoint(base_url: str | None) -> str | None:
    """校验回环地址并返回规范化的服务商地址；不解析 DNS、不走代理（D101）。"""
    if not base_url:
        return None
    try:
        url = httpx.URL(base_url)
        host = "127.0.0.1" if url.host == "localhost" else url.host
        if (
            url.scheme not in ("http", "https")
            or url.userinfo
            or url.query
            or url.fragment
            or not ip_address(host).is_loopback
            or "%" in host
        ):
            return None
    except (ValueError, httpx.InvalidURL):
        return None
    return str(url.copy_with(host=host)).rstrip("/")


def local_expression_endpoint(route: ModelRoute) -> str | None:
    """只接受明确的免费 OpenAI 兼容回环端点；不用代理、跳转或 DNS（D101）。"""
    provider, model = route.provider, route.model
    if (
        model.kind != "embedding"
        or provider.protocol not in ("openai", "openai_responses")
        or any((model.price_in, model.price_out, model.price_cache_in))
    ):
        return None
    return _loopback_endpoint(provider.base_url)


def local_chat_endpoint(route: ModelRoute) -> str | None:
    """回环地址的 OpenAI 兼容对话服务商直连 `/chat/completions`（D131）。

    只接受 openai 协议：openai_responses 走的是另一套接口，保持原路径。
    """
    if route.model.kind != "llm" or route.provider.protocol != "openai":
        return None
    return _loopback_endpoint(route.provider.base_url)


def _read_embedding_batch(data: Any, size: int) -> tuple[tuple[float, ...], ...]:
    if not isinstance(data, list) or len(data) != size:
        return ()
    vectors: dict[int, tuple[float, ...]] = {}
    for position, item in enumerate(data):
        index = item.get("index", position)
        values = item.get("embedding")
        if type(index) is not int or not 0 <= index < size or index in vectors:
            return ()
        if (
            not isinstance(values, list)
            or not values
            or not all(type(value) in (int, float) and isfinite(value) for value in values)
        ):
            return ()
        vectors[index] = tuple(float(value) for value in values)
    if len({len(vector) for vector in vectors.values()}) != 1:
        return ()
    return tuple(vectors[index] for index in range(size))


# 调用方没给背景时的系统提示词：有些服务商不收空的系统消息。
EMPTY_SYSTEM_MESSAGE = PromptTemplate(
    "empty_system", "无背景时的系统提示", "你是 Aureus 的一个器官，按用户消息里的要求回答。"
)


def order_routes[T](routes: Sequence[T], preference: ModelPreference) -> list[T]:
    """按偏好排出尝试的顺序（D59）。

    从偏好对应的位置开始往后（更便宜）试，都不行再从那里往前（更贵）试。
    比如四个模型、从第二个开始：2、3、4、1。只有一个模型时，什么偏好都只用它。

    Args:
        routes: 配置里的模型，按从贵到便宜排。
        preference: 从哪里开始。

    Returns:
        尝试的顺序，每个模型正好出现一次。
    """
    if not routes:
        return []
    last = len(routes) - 1
    start = {
        ModelPreference.PREFERRED: 0,
        ModelPreference.CHEAPER: min(1, last),
        ModelPreference.CHEAPEST: last,
    }[preference]
    return [*routes[start:], *reversed(routes[:start])]


def assemble_messages(
    request: OrganRequest, prompts: PromptStore | None = None
) -> list[dict[str, str]]:
    """预览与实际发送共用消息顺序、角色和模板快照（D90）。"""
    overrides = prompts.snapshot() if prompts is not None else {}
    system = render_prompt(request.system_context, overrides).strip()
    return [
        {
            "role": "system",
            "content": system or render_prompt(EMPTY_SYSTEM_MESSAGE.fill(), overrides),
        },
        *(
            {"role": role, "content": render_prompt(content, overrides)}
            for role, content in request.preceding_messages
        ),
        {"role": "user", "content": render_prompt(request.prompt, overrides)},
    ]


def assemble_image_messages(
    messages: list[dict[str, str]], image_part: dict[str, Any]
) -> list[dict[str, Any]]:
    """在最后一条用户消息中依次放置文字与图片部分；预览传入占位而非图片载荷。"""
    return [
        *messages[:-1],
        {
            "role": messages[-1]["role"],
            "content": [{"type": "text", "text": messages[-1]["content"]}, image_part],
        },
    ]


def conversational_routes(organ_id: str, routes: Sequence[ModelRoute]) -> tuple[ModelRoute, ...]:
    """按器官职责筛选配置好的对话、视觉和语音路线。"""
    if organ_id == "voice":
        return tuple(
            route
            for route in routes
            if route.model.kind == "transcription"
            and route.provider.protocol in ("openai", "openai_responses")
        )
    return tuple(
        route
        for route in routes
        if route.model.kind == "llm" and (organ_id != "vlm" or route.model.vision)
    )


class _SpeechFile(BytesIO):
    name = "voice.mp3"


def _probe_audio() -> BytesIO:
    class ProbeAudio(BytesIO):
        name = "probe.wav"

    audio = ProbeAudio()
    sample_rate = 16_000
    with wave.open(audio, "wb") as recording:
        recording.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        recording.writeframes(bytes(2 * sample_rate))
    audio.seek(0)
    return audio


class LLMOrgan:
    """调用大模型的真实器官：按 models.toml 里的顺序尝试模型，失败就顺延。"""

    def __init__(
        self,
        organ_id: str,
        routes: Sequence[ModelRoute],
        key_resolver: KeyResolver,
        completion: CompletionFunction | None = None,
        token_counter: TokenCounter | None = None,
        sleep: SleepFunction | None = None,
        *,
        prompts: PromptStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        llm_routes = conversational_routes(organ_id, routes)
        if routes and not llm_routes:
            # 启动时就报错，而不是等到第一次调用才发现这个器官无模型可用。
            raise ConfigError(f'器官 {organ_id} 没有可调用的对话模型（需要 kind = "llm" 的模型）。')
        self.organ_id = organ_id
        self._routes = llm_routes
        self._key_resolver = key_resolver
        # 非空真实器官在 prepare 工作线程加载，不能把首次导入留到事件循环。
        self._completion = completion
        if self._completion is None and llm_routes:
            self._completion = (
                _import_litellm().atranscription
                if organ_id == "voice"
                else _load_litellm_completion()
            )
        self._token_counter = token_counter or count_prompt_tokens
        self._sleep = sleep or asyncio.sleep
        self._prompts = prompts
        # 测试用的假网络；本地回环对话直连时传给共享客户端（D131）。
        self._transport = transport

    @classmethod
    def from_config(
        cls,
        organ_id: str,
        config: ModelsConfig,
        key_resolver: KeyResolver,
        completion: CompletionFunction | None = None,
        token_counter: TokenCounter | None = None,
        sleep: SleepFunction | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        """按 models.toml 的配置创建某个器官。

        Args:
            organ_id: 器官名，比如 planner、replyer。
            config: 校验过的模型配置。
            key_resolver: 把环境变量名换成密钥的解析器。
            completion: 调用大模型的函数；不传就用 litellm。
            token_counter: 估算输入 token 数的函数；不传就用 litellm 的计数工具。
            sleep: 重试之间的等待函数；不传就用 asyncio.sleep。
            transport: 测试用的假网络；正式运行不传。

        Returns:
            这个器官的 LLMOrgan。

        Raises:
            ConfigError: 配置里没有这个器官，或者它没有对话模型。
        """
        return cls(
            organ_id,
            config.routes_for(organ_id),
            key_resolver,
            completion,
            token_counter,
            sleep,
            transport=transport,
        )

    async def process(self, request: OrganRequest) -> OrganResponse:
        """按偏好排好顺序（见 order_routes）尝试模型，返回第一个成功的回复。

        Args:
            request: 提示词和生物状态；生物状态只用来生成系统提示词。

        Returns:
            器官的建议。attempts 里是真正发出去的每一次请求，交给网关记账；
            metadata 里有模型名、token 用量和费用，给人看。

        Raises:
            OrganCallError: 所有模型都调用失败。它的 attempts 里同样带着每一次请求。
        """
        if not self._routes:
            raise OrganCallError(self.organ_id, ["未配置可用的模型"], [])
        media = request.media_bytes
        media_type = request.media_type
        if self.organ_id == "voice" and (not media or media_type != "audio/mpeg"):
            raise OrganCallError(self.organ_id, ["没有可转写的 MP3 语音"])
        if self.organ_id == "vlm" and (
            not media or media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp")
        ):
            raise OrganCallError(self.organ_id, ["没有可识别的图片"])
        messages = assemble_messages(request, self._prompts)
        failures: list[str] = []
        attempts: list[CallAttempt] = []
        for route in order_routes(self._routes, request.model_preference):
            try:
                text, tool_calls, attempt = await self._call(route, messages, attempts, request)
            except asyncio.CancelledError:
                if request.stream:
                    raise OrganCallCancelled(attempts) from None
                raise
            except _RouteFailureError as failure:
                failures.append(f"{route.model.name}：{failure.reason}")
                logger.warning(
                    "器官 {} 的模型 {} 没能用：{}", self.organ_id, route.model.name, failure.reason
                )
                continue
            return self._build_response(route, text, tool_calls, attempt, failures, attempts)
        raise OrganCallError(self.organ_id, failures, attempts)

    def _estimate_usage(
        self, route: ModelRoute, messages: list[dict[str, str]], requests: int, extra_bytes: int = 0
    ) -> TokenUsage:
        """服务商没告诉我们用了多少时，按 06 号文档 12.3 节往多里估。

        Args:
            route: 这次走的模型路线，取 max_tokens 用。
            messages: 发出去的消息。
            requests: 估计实际发了几次请求。
            extra_bytes: 附件及工具协议的字节数，只在服务商没有提供用量时保守估算。

        Returns:
            估算的用量，known 为 False。
        """
        try:
            counted: object = self._token_counter(messages)
        except Exception:
            counted = None
        # 计数工具出错或给出的数不合理时，按字节数记：一个 token 至少对应一个字节，只多不少。
        if isinstance(counted, int) and not isinstance(counted, bool) and counted > 0:
            prompt_tokens = counted
        else:
            prompt_tokens = _byte_length(messages)
        completion_tokens = route.max_tokens or ASSUMED_COMPLETION_TOKENS
        return TokenUsage(
            prompt_tokens=(prompt_tokens + extra_bytes) * requests,
            completion_tokens=completion_tokens * requests,
            known=False,
        )

    def _attempt(
        self,
        route: ModelRoute,
        usage: TokenUsage,
        failure_reason: str | None = None,
    ) -> CallAttempt:
        # 确知一个 token 都没用（比如被服务商明确拒绝）时费用就是 0，没写价格也一样，
        # 不是“算不出来”。
        nothing_used = usage.known and usage.prompt_tokens + usage.completion_tokens == 0
        return CallAttempt(
            model=route.model.name,
            provider=route.provider.name,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_tokens=usage.cached_tokens,
            estimated=not usage.known,
            succeeded=failure_reason is None,
            failure_reason=failure_reason,
            cost=0.0 if nothing_used else compute_cost(route.model, usage),
        )

    async def _call(
        self,
        route: ModelRoute,
        messages: list[dict[str, str]],
        attempts: list[CallAttempt],
        request: OrganRequest,
    ) -> tuple[str, tuple[ToolCall, ...] | None, CallAttempt]:
        """对一条路线发请求，临时性的失败按 max_retry 重试。

        重试由这里自己做，不交给 litellm（num_retries 固定为 0）：litellm 内部重试了
        几次看不到，账本只能按最多次估算；自己重试，每一次真正发出去的请求都有一条
        独立的记录（审查 H4）。

        Args:
            route: 这次走的模型路线。
            messages: 发出去的消息。
            attempts: 失败的请求记在这里；成功的那次由返回值带出。
            request: 本轮的附件、工具和流式设置。

        Returns:
            回复文字、已解析的工具调用，以及成功的那次请求。

        Raises:
            _RouteFailureError: 这条路线没能用（重试用完、不值得重试，或密钥缺失）。
        """
        provider = route.provider
        media = request.media_bytes
        media_type = request.media_type
        kwargs: dict[str, Any] = {
            **_provider_arguments(provider, self._key_resolver),
            "model": LITELLM_PREFIX_BY_PROTOCOL[provider.protocol] + route.model.identifier,
            "messages": messages,
        }
        if self.organ_id == "voice":
            assert media is not None
            kwargs["model"] = "openai/" + route.model.identifier
            del kwargs["messages"]
        elif self.organ_id == "vlm":
            assert media is not None and media_type is not None
            data_url = f"data:{media_type};base64,{base64.b64encode(media).decode('ascii')}"
            kwargs["messages"] = assemble_image_messages(
                messages, {"type": "image_url", "image_url": {"url": data_url}}
            )
        if route.temperature is not None and self.organ_id != "voice":
            kwargs["temperature"] = route.temperature
        if route.max_tokens is not None and self.organ_id != "voice":
            kwargs["max_tokens"] = route.max_tokens
        if route.reasoning_effort is not None and self.organ_id != "voice":
            kwargs["reasoning_effort"] = route.reasoning_effort
        if request.stream:
            kwargs["stream"] = True
            if provider.protocol == "openai":
                kwargs["stream_options"] = {"include_usage": True}
        tool_names: set[str] = set()
        extra_bytes = len(media or b"")
        if request.tools and self.organ_id != "voice":
            tools = encode_tools(request.tools)
            history = encode_tool_history(request.tool_history)
            kwargs["tools"] = tools
            kwargs["messages"] = [*kwargs["messages"], *history]
            tool_names = {tool.name for tool in request.tools}
            extra_bytes += len(json.dumps((tools, history), ensure_ascii=False).encode("utf-8"))
        requests = 1 + provider.max_retry
        for number in range(1, requests + 1):
            try:
                if self.organ_id == "voice":
                    assert media is not None
                    kwargs["file"] = _SpeechFile(media)
                return await self._request(
                    route, kwargs, messages, attempts, extra_bytes, tool_names
                )
            except _RouteFailureError as failure:
                if not failure.retryable or number == requests:
                    raise
                logger.warning(
                    "器官 {} 的模型 {} 第 {} 次请求失败（{}），准备重试",
                    self.organ_id,
                    route.model.name,
                    number,
                    failure.reason,
                )
                delay = min(RETRY_BACKOFF_SECONDS * 2 ** (number - 1), MAX_BACKOFF_SECONDS)
                await self._sleep(delay)
        raise AssertionError("max_retry 不会是负数，循环至少执行一次")  # pragma: no cover

    async def _request(
        self,
        route: ModelRoute,
        kwargs: dict[str, Any],
        messages: list[dict[str, str]],
        attempts: list[CallAttempt],
        extra_bytes: int,
        tool_names: set[str],
    ) -> tuple[str, tuple[ToolCall, ...] | None, CallAttempt]:
        """真正发出一次请求；流式没有正文时再发一次非流式重试（D127）。

        失败的请求先记进 attempts，再抛出。
        """
        tools = ToolCallCollector(tool_names) if tool_names else None
        reasoning_only = False
        try:
            completion = self._completion
            assert completion is not None  # 无可用路线时 process 已拒绝，不会走到这里。
            response = await _send_chat(
                route, completion, kwargs, transport=self._transport
            )
            if kwargs.get("stream"):
                text, usage, reasoning_only = await _read_stream(response, tools)
                if text is None and (tools is None or tools.finish() == ()):
                    # 流式没有正文、也没有工具调用：同一请求去掉 stream 再试一次（D127）。
                    # 流式那一次照样记账；重试的用量以非流式响应为准。
                    if not usage.known:
                        usage = self._estimate_usage(route, messages, 1, extra_bytes)
                    attempts.append(
                        self._attempt(
                            route,
                            usage,
                            REASONING_ONLY_REASON if reasoning_only else EMPTY_REPLY_REASON,
                        )
                    )
                    retry_kwargs = {
                        key: value
                        for key, value in kwargs.items()
                        if key not in ("stream", "stream_options")
                    }
                    response = await _send_chat(
                        route, completion, retry_kwargs, transport=self._transport
                    )
                    usage = extract_usage(response)
                    text = _read_text(response)
                    reasoning_only = reasoning_only or _read_reasoning(response)
                    if tools is not None and (choices := getattr(response, "choices", None)):
                        tools.read_choice(choices[0], stream=False)
            else:
                usage = extract_usage(response)
                if self.organ_id == "voice":
                    value = getattr(response, "text", None)
                    text = value.strip() if isinstance(value, str) and value.strip() else None
                else:
                    text = _read_text(response)
                    reasoning_only = _read_reasoning(response)
                    if tools is not None and (choices := getattr(response, "choices", None)):
                        tools.read_choice(choices[0], stream=False)
        except asyncio.CancelledError:
            if kwargs.get("stream"):
                usage = self._estimate_usage(route, messages, 1, extra_bytes)
                attempts.append(self._attempt(route, usage, "新消息或运行控制中止流式请求"))
            raise
        except Exception as error:
            reason = _failure_reason(error)
            if _rejected_before_processing(error):
                # 服务商明确拒绝，没有处理这个请求：不产生用量（06 号文档 12.3 节）。
                attempt = self._attempt(route, TokenUsage(), reason)
            else:
                # 超时、断线这类失败，对方可能已经算完了：按一次请求估算。
                attempt = self._attempt(
                    route, self._estimate_usage(route, messages, 1, extra_bytes), reason
                )
            attempts.append(attempt)
            # from None：不带上原始异常，免得追踪信息里带出请求头里的密钥。
            raise _RouteFailureError(reason, retryable=_retryable(error)) from None
        if not usage.known:
            usage = self._estimate_usage(route, messages, 1, extra_bytes)
        tool_calls = tools.finish() if tools is not None else ()
        if text is None and tool_calls == ():
            # 已经用掉了 token 却没有可用内容（比如推理模型把 token 都花在
            # 思考上），这次请求照样要记账。重试只会再花一次钱，所以不重试。
            reason = REASONING_ONLY_REASON if reasoning_only else EMPTY_REPLY_REASON
            attempts.append(self._attempt(route, usage, reason))
            raise _RouteFailureError(reason)
        suggestion = (text or "") if tool_calls is not None else ""
        return suggestion, tool_calls, self._attempt(route, usage)

    def _build_response(
        self,
        route: ModelRoute,
        text: str,
        tool_calls: tuple[ToolCall, ...] | None,
        attempt: CallAttempt,
        failures: list[str],
        failed_attempts: list[CallAttempt],
    ) -> OrganResponse:
        return OrganResponse(
            organ_id=self.organ_id,
            suggestion=text,
            attempts=[*failed_attempts, attempt],
            tool_calls=tool_calls or (),
            metadata={
                "type": "llm",
                "model": route.model.name,
                "provider": route.provider.name,
                "prompt_tokens": attempt.prompt_tokens,
                "completion_tokens": attempt.completion_tokens,
                "cached_tokens": attempt.cached_tokens,
                "usage_known": not attempt.estimated,
                # 费用单位是元：cost 是这次成功调用的，wasted_cost 是前面
                # 失败的请求已经产生的。两者为 None 表示算不出来（没写价格，或用量
                # 是估算的），不是 0（D37）。
                "cost": attempt.cost,
                "wasted_cost": sum_costs(failed.cost for failed in failed_attempts),
                "failures_before_success": failures,
                **({"tool_calls_invalid": True} if tool_calls is None else {}),
            },
        )
