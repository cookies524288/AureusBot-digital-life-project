"""器官层：真实器官，通过 litellm 调用大模型（D24）。

一个 LLMOrgan 对应 models.toml 里的一个器官（planner、replyer……），
按配置里的顺序尝试它的模型：前一个失败（密钥缺失、超时、限流、
返回为空……）才顺延到下一个，全部失败就抛 OrganCallError，
由调用方决定怎么降级。

器官只返回建议，不写任何状态：身体状态只读，被翻译成系统提示词里
的几句话；每一次真正发出去的请求（含失败的）都放进 attempts，交给安全网关记账（D39）。
"""

import os
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Self

from loguru import logger

from aureus.common.config import ConfigError
from aureus.core.biomodel import BiologicalState
from aureus.core.cognitive_layer import CognitiveObserver
from aureus.organs.api_keys import KeyResolver
from aureus.organs.interface import (
    CallAttempt,
    OrganCallError,
    OrganRequest,
    OrganResponse,
    sum_costs,
)
from aureus.organs.llm_usage import TokenUsage, compute_cost, extract_usage
from aureus.organs.model_config import ModelRoute, ModelsConfig, ProviderProtocol

# 调用大模型的函数。默认是 litellm.acompletion；测试时换成假的，不联网。
CompletionFunction = Callable[..., Awaitable[Any]]
# 估算输入 token 数的函数。默认是 count_prompt_tokens；测试时换成假的，结果固定。
TokenCounter = Callable[[list[dict[str, str]]], int]

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
    "RateLimitError": "被限流",
    "AuthenticationError": "认证失败（密钥无效？）",
    "ContextWindowExceededError": "超出上下文长度",
    "APIConnectionError": "连不上服务商",
    "ServiceUnavailableError": "服务商暂时不可用",
    "InternalServerError": "服务商内部错误",
    "BadRequestError": "请求被拒绝（参数不被支持？）",
}

EMPTY_REPLY_REASON = "返回内容为空或格式异常"

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

# 没配置 max_tokens、又没返回用量时，输出按这么多 token 记。
# 宁可多记（D37）：常见对话模型默认的最大输出长度，大多不超过它。
ASSUMED_COMPLETION_TOKENS = 8192


class _RouteFailureError(Exception):
    """某一条路线没能用。attempt 是这次真正发出去的请求；密钥缺失、根本没发出去时是 None。"""

    def __init__(self, reason: str, attempt: CallAttempt | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempt = attempt


def _failure_reason(error: Exception) -> str:
    # 沿继承链找：litellm 的 Timeout 继承自 APIConnectionError，
    # 先匹配到更具体的那个。
    for error_class in type(error).__mro__:
        reason = FAILURE_REASON_BY_ERROR_NAME.get(error_class.__name__)
        if reason is not None:
            return reason
    return f"未知错误（{type(error).__name__}）"


def _rejected_before_processing(error: Exception) -> bool:
    return any(
        error_class.__name__ in REJECTED_BEFORE_PROCESSING_ERROR_NAMES
        for error_class in type(error).__mro__
    )


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


def _load_litellm_completion() -> CompletionFunction:
    # 创建真实器官发生在启动阶段，不在心跳循环里，导入慢一点不要紧。
    return _import_litellm().acompletion


def build_system_prompt(bio_state: BiologicalState) -> str:
    """把身体状态翻译成系统提示词（只读，不改状态）。

    Args:
        bio_state: 当前的生物状态。

    Returns:
        系统提示词：几句话描述真实的精力、激素和情绪。
    """
    described = "".join(
        sentence
        for sentence in (
            CognitiveObserver.system_prompt_modifier(bio_state),
            bio_state.compute_emotion().system_prompt_modifier(),
        )
        if sentence
    )
    return (
        "下面是你此刻真实的身体和情绪状态，由生物模型算出，不是设定：\n"
        f"{described}\n"
        "让语气和回复的长短自然地体现这些状态，不要编造这里没写到的身体感受。"
    )


def _system_message(request: OrganRequest) -> str:
    """系统提示词：调用方给的背景在前，身体状态在后（D41 第 8 条）。"""
    body = build_system_prompt(request.bio_state)
    background = request.system_context.strip()
    return f"{background}\n\n{body}" if background else body


class LLMOrgan:
    """调用大模型的真实器官：按 models.toml 里的顺序尝试模型，失败就顺延。"""

    def __init__(
        self,
        organ_id: str,
        routes: Sequence[ModelRoute],
        key_resolver: KeyResolver,
        completion: CompletionFunction | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        llm_routes = tuple(route for route in routes if route.model.kind == "llm")
        if not llm_routes:
            # 启动时就报错，而不是等到第一次调用才发现这个器官无模型可用。
            raise ConfigError(f'器官 {organ_id} 没有可调用的对话模型（需要 kind = "llm" 的模型）。')
        self.organ_id = organ_id
        self._routes = llm_routes
        self._key_resolver = key_resolver
        self._completion = completion or _load_litellm_completion()
        self._token_counter = token_counter or count_prompt_tokens

    @classmethod
    def from_config(
        cls,
        organ_id: str,
        config: ModelsConfig,
        key_resolver: KeyResolver,
        completion: CompletionFunction | None = None,
        token_counter: TokenCounter | None = None,
    ) -> Self:
        """按 models.toml 的配置创建某个器官。

        Args:
            organ_id: 器官名，比如 planner、replyer。
            config: 校验过的模型配置。
            key_resolver: 把环境变量名换成密钥的解析器。
            completion: 调用大模型的函数；不传就用 litellm。
            token_counter: 估算输入 token 数的函数；不传就用 litellm 的计数工具。

        Returns:
            这个器官的 LLMOrgan。

        Raises:
            ConfigError: 配置里没有这个器官，或者它没有对话模型。
        """
        return cls(organ_id, config.routes_for(organ_id), key_resolver, completion, token_counter)

    async def process(self, request: OrganRequest) -> OrganResponse:
        """按顺序尝试模型，返回第一个成功的回复。

        Args:
            request: 提示词和生物状态；生物状态只用来生成系统提示词。

        Returns:
            器官的建议。attempts 里是真正发出去的每一次请求，交给网关记账；
            metadata 里有模型名、token 用量和费用，给人看。

        Raises:
            OrganCallError: 所有模型都调用失败。它的 attempts 里同样带着每一次请求。
        """
        messages = [
            {"role": "system", "content": _system_message(request)},
            {"role": "user", "content": request.prompt},
        ]
        failures: list[str] = []
        attempts: list[CallAttempt] = []
        for route in self._routes:
            try:
                text, attempt = await self._call(route, messages)
            except _RouteFailureError as failure:
                failures.append(f"{route.model.name}：{failure.reason}")
                if failure.attempt is not None:
                    attempts.append(failure.attempt)
                logger.warning(
                    "器官 {} 的模型 {} 没能用：{}", self.organ_id, route.model.name, failure.reason
                )
                continue
            return self._build_response(route, text, attempt, failures, attempts)
        raise OrganCallError(self.organ_id, failures, attempts)

    def _estimate_usage(
        self, route: ModelRoute, messages: list[dict[str, str]], requests: int
    ) -> TokenUsage:
        """服务商没告诉我们用了多少时，按 06 号文档 12.3 节往多里估。

        Args:
            route: 这次走的模型路线，取 max_tokens 用。
            messages: 发出去的消息。
            requests: 估计实际发了几次请求（litellm 内部重试的次数看不到，按最多算）。

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
            prompt_tokens=prompt_tokens * requests,
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
        self, route: ModelRoute, messages: list[dict[str, str]]
    ) -> tuple[str, CallAttempt]:
        provider = route.provider
        kwargs: dict[str, Any] = {
            "model": LITELLM_PREFIX_BY_PROTOCOL[provider.protocol] + route.model.identifier,
            "messages": messages,
            "timeout": provider.timeout_seconds,
            # 重试交给 litellm，次数来自 models.toml 的 max_retry。
            "num_retries": provider.max_retry,
        }
        if provider.base_url is not None:
            kwargs["api_base"] = provider.base_url
        if provider.api_key_env is not None:
            key = self._key_resolver.get(provider.api_key_env)
            if key is None:
                # 调用前就拒绝，不发出空请求（见 15 号文档第 4 节）。
                raise _RouteFailureError(f"密钥缺失（{provider.api_key_env} 没有填写）")
            kwargs["api_key"] = key.get_secret_value()
        if route.temperature is not None:
            kwargs["temperature"] = route.temperature
        if route.max_tokens is not None:
            kwargs["max_tokens"] = route.max_tokens
        try:
            response = await self._completion(**kwargs)
        except Exception as error:
            reason = _failure_reason(error)
            if _rejected_before_processing(error):
                # 服务商明确拒绝，没有处理这个请求：不产生用量（06 号文档 12.3 节）。
                attempt = self._attempt(route, TokenUsage(), reason)
            else:
                # 超时、断线这类失败，对方可能已经算完了，litellm 也可能重试过，
                # 次数看不到，按最多 1 + max_retry 次请求估算。
                estimate = self._estimate_usage(route, messages, 1 + provider.max_retry)
                attempt = self._attempt(route, estimate, reason)
            # from None：不带上原始异常，免得追踪信息里带出请求头里的密钥。
            raise _RouteFailureError(reason, attempt) from None
        usage = extract_usage(response)
        if not usage.known:
            usage = self._estimate_usage(route, messages, requests=1)
        text = _read_text(response)
        if text is None:
            # 已经用掉了 token 却没有可用内容（比如推理模型把 token 都花在
            # 思考上），这次请求照样要记账，所以带在失败里交给上一层。
            raise _RouteFailureError(
                EMPTY_REPLY_REASON, self._attempt(route, usage, EMPTY_REPLY_REASON)
            )
        return text, self._attempt(route, usage)

    def _build_response(
        self,
        route: ModelRoute,
        text: str,
        attempt: CallAttempt,
        failures: list[str],
        failed_attempts: list[CallAttempt],
    ) -> OrganResponse:
        return OrganResponse(
            organ_id=self.organ_id,
            suggestion=text,
            attempts=[*failed_attempts, attempt],
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
            },
        )
