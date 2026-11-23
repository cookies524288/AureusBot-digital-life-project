"""器官接口与消息协议"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class CallPurpose(StrEnum):
    """这次调用是为了什么。安全网关按它决定精力不够时放不放行（04 号文档第 4 节，D56）。"""

    # 回应用户：用户开口了，精力再低也要回应（只受月度上限约束）。
    USER_REPLY = "回应用户"
    # 可选的事：探索、闲聊、整理等没人在等的调用。默认值，忘了标时按这个处理，
    # 不确定就朝省的方向倒。
    OPTIONAL = "可选"


class ModelPreference(StrEnum):
    """从器官的模型列表哪里开始用（15 号文档第 5 节，D59）。

    列表按从贵到便宜排；精力越低，越从后面开始。开始的那个不行时，先往后找更便宜的，
    都不行再往前找更贵的：先省钱，实在不行也要回上话（月度上限仍由安全网关兜底）。
    """

    # 第一个，正常档用。
    PREFERRED = "首选"
    # 第二个（只有一个模型时就是它），节省档用。
    CHEAPER = "便宜一档"
    # 最后一个，低电量和透支保护用。
    CHEAPEST = "最便宜"


@dataclass
class OrganRequest:
    """发送给器官的请求"""

    organ_id: str  # "replyer" / "planner" / "judge" / ...
    prompt: str  # 原始提示词
    context: dict[str, Any] = field(default_factory=dict)  # 额外上下文
    # 调用方给的背景（身份、人设、现在几点、醒来信息），器官放在系统提示词最前面（D41 第 8 条）。
    system_context: str = ""
    # 为什么调用；调用方知道是回应用户时必须明确写 USER_REPLY。
    purpose: CallPurpose = CallPurpose.OPTIONAL
    # 从模型列表哪里开始用；调用方按精力档位选（D59）。不调用模型的器官忽略它。
    model_preference: ModelPreference = ModelPreference.PREFERRED
    # 文本嵌入传字符串，图片嵌入传已读取的图片字节。
    embedding_inputs: tuple[str | bytes, ...] = ()
    media_bytes: bytes | None = field(default=None, repr=False)
    media_type: str | None = None


@dataclass(frozen=True)
class CallAttempt:
    """一次真正发出去的模型请求，安全网关按它记账（D39，06 号文档 12.2 节）。

    密钥缺失等根本没发出去的请求不算。器官调用了模型却不报告，账本就会漏记，
    所以每个调用模型的器官都必须如实报告。
    """

    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    # 输入里命中缓存的部分，已经包含在 prompt_tokens 里。
    cached_tokens: int = 0
    # 服务商没返回用量时为 True：上面的数字是按 06 号文档 12.3 节估算的。
    estimated: bool = False
    succeeded: bool = True
    failure_reason: str | None = None
    # 费用（元），只用来显示；None 表示算不出来（没写价格，或用量是估算的），不是 0（D37）。
    cost: float | None = None

    def __post_init__(self) -> None:
        for name in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            value = getattr(self, name)
            # bool 是 int 的子类，True 会被当成 1，这里单独挡掉。
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} 必须是不小于 0 的整数，但收到了 {value!r}")

    @property
    def budget_tokens(self) -> int:
        """计入预算的 token：输入加输出。缓存命中已经包含在输入里，不重复计算。"""
        return self.prompt_tokens + self.completion_tokens


def sum_costs(costs: Iterable[float | None]) -> float | None:
    """把几笔费用加起来；有一笔算不出来，总数就算不出来（D37）。

    Args:
        costs: 每一笔费用，None 表示算不出来。

    Returns:
        总费用；有任何一笔是 None 时返回 None。
    """
    total = 0.0
    for cost in costs:
        if cost is None:
            return None
        total += cost
    return total


@dataclass
class OrganResponse:
    """器官返回的响应"""

    organ_id: str
    suggestion: str  # 器官的建议或生成内容
    confidence: float = 0.5  # [0, 1] 置信度
    metadata: dict[str, Any] = field(default_factory=dict)  # token 数、模型名等
    # 这次调用里真正发出去的每一次模型请求，包括成功之前失败的那些。不调用模型的器官留空。
    attempts: list[CallAttempt] = field(default_factory=list)
    embeddings: tuple[tuple[float, ...], ...] = ()


class OrganCallError(Exception):
    """器官的所有候选模型都调用失败。

    消息是中文，逐个说明每个模型失败的原因；不含密钥和提示词内容。
    调用方（网关、核心）捕获它之后决定怎么降级，器官自己不做默认回复。
    """

    def __init__(
        self, organ_id: str, failures: list[str], attempts: Sequence[CallAttempt] = ()
    ) -> None:
        self.organ_id = organ_id
        self.failures = failures
        # 失败的请求也可能已经用掉了 token，账本照样要记（D39）。
        self.attempts = list(attempts)
        super().__init__(f"器官 {organ_id} 的所有模型都调用失败：" + "；".join(failures))

    @property
    def wasted_cost(self) -> float | None:
        """失败的请求里已经产生的费用（元）；None 表示算不出来，不是 0（D37）。"""
        return sum_costs(attempt.cost for attempt in self.attempts)


class Organ(Protocol):
    """器官的接口协议"""

    organ_id: str

    async def process(self, request: OrganRequest) -> OrganResponse:
        """处理请求并返回响应

        Args:
            request: 包含提示词、生物状态和上下文的请求

        Returns:
            response: 器官的建议（不修改自己的状态）
        """
        ...
