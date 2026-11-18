"""器官从模型列表哪里开始用的测试（路线图 1.6，D59）：先省钱，不行再往贵的找。"""

import pytest

from aureus.organs import ModelPreference
from aureus.organs.llm_adapter import order_routes

MODELS = ["贵", "中", "便宜", "最便宜"]


@pytest.mark.parametrize(
    ("preference", "expected"),
    [
        (ModelPreference.PREFERRED, ["贵", "中", "便宜", "最便宜"]),
        (ModelPreference.CHEAPER, ["中", "便宜", "最便宜", "贵"]),
        (ModelPreference.CHEAPEST, ["最便宜", "便宜", "中", "贵"]),
    ],
)
def test_order_starts_at_the_preference_then_goes_cheaper_then_dearer(
    preference: ModelPreference, expected: list[str]
) -> None:
    assert order_routes(MODELS, preference) == expected


@pytest.mark.parametrize("preference", list(ModelPreference))
@pytest.mark.parametrize("count", [1, 2, 3, 5])
def test_every_model_appears_exactly_once(preference: ModelPreference, count: int) -> None:
    routes = list(range(count))
    assert sorted(order_routes(routes, preference)) == routes


@pytest.mark.parametrize("preference", list(ModelPreference))
def test_a_single_model_is_always_used(preference: ModelPreference) -> None:
    assert order_routes(["唯一"], preference) == ["唯一"]


@pytest.mark.parametrize("preference", list(ModelPreference))
def test_no_models_gives_nothing(preference: ModelPreference) -> None:
    assert order_routes([], preference) == []


def test_two_models_cheaper_and_cheapest_both_start_with_the_second() -> None:
    assert order_routes(["贵", "便宜"], ModelPreference.CHEAPER) == ["便宜", "贵"]
    assert order_routes(["贵", "便宜"], ModelPreference.CHEAPEST) == ["便宜", "贵"]
