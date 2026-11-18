"""把 litellm 返回的用量换算成费用的测试。"""

from types import SimpleNamespace

import pytest

from aureus.organs.llm_usage import TokenUsage, compute_cost, extract_usage
from aureus.organs.model_config import ModelConfig


def make_model(price_cache_in: float | None = None) -> ModelConfig:
    return ModelConfig(
        name="m",
        identifier="m-id",
        provider="p",
        price_in=4.0,
        price_out=8.0,
        price_cache_in=price_cache_in,
    )


def make_response(prompt: object, completion: object, details: object = None) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=details
    )
    return SimpleNamespace(usage=usage)


def test_cost_without_cache() -> None:
    cost = compute_cost(make_model(), TokenUsage(prompt_tokens=1000, completion_tokens=100))
    assert cost == pytest.approx(0.0048)


def test_cached_tokens_use_the_cache_price() -> None:
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    assert compute_cost(make_model(price_cache_in=1.0), usage) == pytest.approx(0.0036)


def test_missing_cache_price_falls_back_to_input_price() -> None:
    # 宁可高估也不低估：没写缓存价格就按普通输入价格算。
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    assert compute_cost(make_model(), usage) == pytest.approx(0.0048)


def test_free_model_costs_nothing() -> None:
    free = ModelConfig(name="f", identifier="f", provider="p", price_in=0, price_out=0)
    assert compute_cost(free, TokenUsage(prompt_tokens=5000, completion_tokens=5000)) == 0.0


def test_missing_prices_mean_unknown_cost_not_free() -> None:
    # D37：价格可选。没写价格不等于免费，费用是“未知”（None），不能记成 0。
    unpriced = ModelConfig(name="u", identifier="u", provider="p")
    usage = TokenUsage(prompt_tokens=5000, completion_tokens=5000)
    assert compute_cost(unpriced, usage) is None


def test_one_missing_price_still_means_unknown_cost() -> None:
    half_priced = ModelConfig(name="h", identifier="h", provider="p", price_in=4.0)
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100)
    assert compute_cost(half_priced, usage) is None


def test_unknown_usage_means_unknown_cost() -> None:
    assert compute_cost(make_model(), TokenUsage(known=False)) is None


def test_extract_usage_reads_cached_tokens() -> None:
    usage = extract_usage(make_response(1000, 100, SimpleNamespace(cached_tokens=400)))
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (1000, 100, 400)
    assert usage.known


def test_missing_cache_details_mean_no_cached_tokens() -> None:
    assert extract_usage(make_response(1000, 100)).cached_tokens == 0


def test_missing_usage_is_marked_unknown() -> None:
    usage = extract_usage(SimpleNamespace())
    assert not usage.known
    assert usage.prompt_tokens == 0


@pytest.mark.parametrize("bad", [None, -5, "12", 3.5, 12.0, True])
def test_untrustworthy_counts_make_the_usage_unknown_not_free(bad: object) -> None:
    # 审查 S2/M3：字段是 None、负数、浮点、bool 时不能记成「已知的 0」。
    usage = extract_usage(make_response(bad, bad, SimpleNamespace(cached_tokens=bad)))
    assert not usage.known
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (0, 0, 0)


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens"])
def test_one_bad_field_makes_the_whole_usage_unknown(field: str) -> None:
    good = {"prompt_tokens": 100, "completion_tokens": 10}
    good[field] = None  # type: ignore[assignment]
    assert not extract_usage(SimpleNamespace(usage=SimpleNamespace(**good))).known


def test_usage_object_without_the_count_fields_is_unknown() -> None:
    assert not extract_usage(SimpleNamespace(usage=SimpleNamespace())).known


def test_all_zero_usage_is_unknown() -> None:
    # 真实请求至少有输入 token，两个数都是 0 说明服务商没填。
    assert not extract_usage(make_response(0, 0)).known


def test_zero_completion_with_real_prompt_is_still_known() -> None:
    usage = extract_usage(make_response(100, 0))
    assert usage.known
    assert usage.prompt_tokens == 100


def test_unreadable_cached_tokens_count_as_zero_but_keep_the_usage_known() -> None:
    usage = extract_usage(make_response(100, 10, SimpleNamespace(cached_tokens=None)))
    assert usage.known
    assert usage.cached_tokens == 0


def test_cached_tokens_never_exceed_prompt_tokens() -> None:
    usage = extract_usage(make_response(100, 10, SimpleNamespace(cached_tokens=500)))
    assert usage.cached_tokens == 100
