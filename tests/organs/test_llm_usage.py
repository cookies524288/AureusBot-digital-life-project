"""把 litellm 返回的用量换算成费用的测试。"""

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aureus.organs.llm_usage import TokenUsage, compute_cost, extract_usage
from aureus.organs.model_config import ModelConfig, PricePeriod

MOMENT = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)


def make_model(price_cache_in: float | None = None, **fields: object) -> ModelConfig:
    return ModelConfig(
        name="m",
        identifier="m-id",
        provider="p",
        price_in=4.0,
        price_out=8.0,
        price_cache_in=price_cache_in,
        **fields,
    )


def period(start: str, end: str, price_in: float, price_out: float, cache: float) -> PricePeriod:
    return PricePeriod(
        start=start, end=end, price_in=price_in, price_out=price_out, price_cache_in=cache
    )


def make_response(prompt: object, completion: object, details: object = None) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=details
    )
    return SimpleNamespace(usage=usage)


def test_cost_without_cache() -> None:
    cost = compute_cost(make_model(), TokenUsage(prompt_tokens=1000, completion_tokens=100), MOMENT)
    assert cost == pytest.approx(0.0048)


def test_cached_tokens_use_the_cache_price() -> None:
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    assert compute_cost(make_model(price_cache_in=1.0), usage, MOMENT) == pytest.approx(0.0036)


def test_missing_cache_price_falls_back_to_input_price() -> None:
    # 宁可高估也不低估：没写缓存价格就按普通输入价格算。
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    assert compute_cost(make_model(), usage, MOMENT) == pytest.approx(0.0048)


def test_free_model_costs_nothing() -> None:
    free = ModelConfig(name="f", identifier="f", provider="p", price_in=0, price_out=0)
    assert compute_cost(free, TokenUsage(prompt_tokens=5000, completion_tokens=5000), MOMENT) == 0.0


def test_missing_prices_mean_unknown_cost_not_free() -> None:
    # D37：价格可选。没写价格不等于免费，费用是“未知”（None），不能记成 0。
    unpriced = ModelConfig(name="u", identifier="u", provider="p")
    usage = TokenUsage(prompt_tokens=5000, completion_tokens=5000)
    assert compute_cost(unpriced, usage, MOMENT) is None


def test_one_missing_price_still_means_unknown_cost() -> None:
    half_priced = ModelConfig(name="h", identifier="h", provider="p", price_in=4.0)
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100)
    assert compute_cost(half_priced, usage, MOMENT) is None


def test_unknown_usage_means_unknown_cost() -> None:
    assert compute_cost(make_model(), TokenUsage(known=False), MOMENT) is None


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


def period_model() -> ModelConfig:
    return make_model(
        price_cache_in=1.0,
        price_periods=(
            period("08:00", "12:00", price_in=10.0, price_out=20.0, cache=5.0),
        ),
    )


def test_price_period_hit_uses_the_period_prices() -> None:
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    assert compute_cost(period_model(), usage, MOMENT, timezone=UTC) == pytest.approx(0.01)


def test_price_period_miss_uses_the_default_prices() -> None:
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    afternoon = datetime(2026, 11, 26, 13, 0, tzinfo=UTC)
    assert compute_cost(period_model(), usage, afternoon, timezone=UTC) == pytest.approx(0.0036)


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (7, 59, 0.0036),  # 时段开始之前：默认价格
        (8, 0, 0.01),  # 左闭：开始那一刻算命中
        (11, 59, 0.01),  # 结束前一分钟仍命中
        (12, 0, 0.0036),  # 右开：结束那一刻不算命中
    ],
)
def test_price_period_boundaries_are_left_closed_right_open(
    hour: int, minute: int, expected: float
) -> None:
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    moment = datetime(2026, 11, 26, hour, minute, tzinfo=UTC)
    assert compute_cost(period_model(), usage, moment, timezone=UTC) == pytest.approx(expected)


def test_price_period_uses_the_local_wall_clock() -> None:
    # UTC 01:00 在上海是 09:00，命中 08:00–12:00 的时段。
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    utc_moment = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)
    shanghai = timezone(timedelta(hours=8))
    assert compute_cost(period_model(), usage, utc_moment, timezone=shanghai) == pytest.approx(
        0.01
    )


def test_price_period_can_supply_prices_when_defaults_are_missing() -> None:
    model = ModelConfig(
        name="m",
        identifier="m-id",
        provider="p",
        price_periods=(
            period("08:00", "12:00", price_in=10.0, price_out=20.0, cache=5.0),
        ),
    )
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400)
    assert compute_cost(model, usage, MOMENT, timezone=UTC) == pytest.approx(0.01)
