"""器官层：把 litellm 返回的用量换算成费用。

费用一律按 models.toml 里写的价格自己算，不用 litellm 自带的价格表：
价格表可能过时、也可能没收录用户的模型。价格是可选的（D37）：账本和预算按 token 计，
费用只用来显示；没写价格时费用是“未知”（None），不会被当成 0。
"""

from dataclasses import dataclass
from typing import Any

from aureus.organs.model_config import ModelConfig

# 价格的单位是“元 / 百万 token”。
TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True)
class TokenUsage:
    """一次调用用掉的 token。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 输入里命中缓存的部分，已经包含在 prompt_tokens 里。
    cached_tokens: int = 0
    # 服务商没返回可信的用量时为 False：extract_usage 这时给出的三个数都是 0，但不代表真的
    # 没花钱。适配器会换成按 06 号文档 12.3 节往多里估的数，known 仍为 False，费用算不出来。
    known: bool = True


def _as_count(value: object) -> int | None:
    """把服务商给的字段读成非负整数；读不出可信的整数时返回 None。

    为什么浮点（12.0）也算读不出：宁可当成「未知」走保守估算，也不静默改写服务商的数；
    bool 是 int 的子类，True 不能当成 1 个 token（审查 S2、M3）。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def extract_usage(response: Any) -> TokenUsage:
    """从 litellm 的返回对象里取出 token 用量。

    任何拿不到可信用量的情况都算「未知」，交给适配器按保守估算记账，绝不记成 0：
    整个 usage 缺失、输入或输出的字段缺失或不是非负整数、两个数都是 0
    （真实请求至少有输入）。命中缓存的字段只影响费用，读不出时当 0（多算费用，不少算）。

    Args:
        response: litellm 的返回对象（ModelResponse）。

    Returns:
        用量；拿不到可信数字时，known 为 False。
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage(known=False)
    prompt = _as_count(getattr(usage, "prompt_tokens", None))
    completion = _as_count(getattr(usage, "completion_tokens", None))
    if prompt is None or completion is None or prompt + completion == 0:
        return TokenUsage(known=False)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = _as_count(getattr(details, "cached_tokens", None)) or 0
    # 命中缓存的部分不可能比整个输入还多，多了就是数据有问题，按输入的总数封顶。
    return TokenUsage(prompt, completion, min(cached, prompt))


def compute_cost(model: ModelConfig, usage: TokenUsage) -> float | None:
    """按模型的价格算一次调用的费用（元）。

    命中缓存的输入按 price_cache_in 算；没写这个价格就按 price_in 算，宁可高估也不低估。
    价格没写全、或者服务商没返回用量时，费用算不出来，返回 None 而不是 0：
    0 会被当成“免费”（D37）。

    Args:
        model: 模型配置，带三个价格（都是可选的）。
        usage: 这次调用的 token 用量。

    Returns:
        费用，单位元；算不出来时为 None。
    """
    price_in = model.price_in
    price_out = model.price_out
    if price_in is None or price_out is None or not usage.known:
        return None
    cache_price = model.price_cache_in if model.price_cache_in is not None else price_in
    fresh_tokens = max(0, usage.prompt_tokens - usage.cached_tokens)
    total = (
        fresh_tokens * price_in
        + usage.cached_tokens * cache_price
        + usage.completion_tokens * price_out
    )
    return total / TOKENS_PER_PRICE_UNIT
