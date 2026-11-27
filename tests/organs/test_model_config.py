"""模型配置（models.toml）的读取与校验测试。"""

import tomllib
from pathlib import Path

import pytest

from aureus.common.config import ConfigError
from aureus.common.paths import AureusPaths
from aureus.organs.model_config import (
    DEFAULT_MAX_RETRY,
    DEFAULT_TIMEOUT_SECONDS,
    MODELS_CONFIG_FORMAT_VERSION,
    load_models_config,
    validate_models_config,
)

# TOML 里的字符串用单引号（字面量字符串），免得和 Python 的双引号混在一起。
VALID_TOML = """
config_version = 1

[[providers]]
name = 'cloud'
protocol = 'openai'
base_url = 'https://api.example.com/v1'
api_key_env = 'AUREUS_KEY_CLOUD'

[[providers]]
name = 'local'
protocol = 'anthropic'
timeout_seconds = 5
max_retry = 0

[[models]]
name = 'big'
identifier = 'big-model'
provider = 'cloud'
price_in = 4.0
price_out = 8.0
temperature = 0.9
max_tokens = 2000

[[models]]
name = 'small'
identifier = 'small-model'
provider = 'local'
price_in = 0
price_out = 0

[organs.replyer]
models = ['big', 'small']
max_tokens = 300
"""


def write_models_toml(tmp_path: Path, text: str) -> AureusPaths:
    paths = AureusPaths(tmp_path)
    paths.ensure_directories()
    paths.models_config_file.write_text(text, encoding="utf-8")
    return paths


def load_error(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as info:
        load_models_config(write_models_toml(tmp_path, text))
    return str(info.value)


def test_valid_file_loads_and_routes_keep_configured_order(tmp_path: Path) -> None:
    config = load_models_config(write_models_toml(tmp_path, VALID_TOML))
    routes = config.routes_for("replyer")
    assert [route.model.name for route in routes] == ["big", "small"]
    assert routes[0].provider.name == "cloud"
    assert routes[1].provider.protocol == "anthropic"
    assert config.models[0].reasoning_levels == ()
    assert config.organs["replyer"].reasoning_effort is None
    assert routes[0].reasoning_effort is None


def test_reasoning_effort_requires_every_model_to_list_the_supported_level(
    tmp_path: Path,
) -> None:
    text = VALID_TOML.replace(
        "max_tokens = 2000", "max_tokens = 2000\nreasoning_levels = ['high']"
    ).replace("max_tokens = 300", "max_tokens = 300\nreasoning_effort = 'high'")

    message = load_error(tmp_path, text)

    assert "器官 replyer 的思考等级 high 不是模型 small 支持的" in message
    assert "没写 reasoning_levels 就是不支持" in message


def test_reasoning_effort_is_carried_to_routes_when_all_models_support_it(
    tmp_path: Path,
) -> None:
    text = (
        VALID_TOML.replace("max_tokens = 2000", "max_tokens = 2000\nreasoning_levels = ['high']")
        .replace(
            "name = 'small'\nidentifier", "name = 'small'\nreasoning_levels = ['high']\nidentifier"
        )
        .replace("max_tokens = 300", "max_tokens = 300\nreasoning_effort = 'high'")
    )

    config = load_models_config(write_models_toml(tmp_path, text))

    assert [route.reasoning_effort for route in config.routes_for("replyer")] == ["high", "high"]


def test_validate_models_config_uses_the_given_source_without_a_local_path(
    tmp_path: Path,
) -> None:
    invalid = VALID_TOML.replace("provider = 'local'", "provider = 'missing'")
    paths = write_models_toml(tmp_path, invalid)

    with pytest.raises(ConfigError) as file_error:
        load_models_config(paths)
    with pytest.raises(ConfigError) as submission_error:
        validate_models_config(tomllib.loads(invalid), "提交的模型配置")

    expected = str(file_error.value).replace(
        f"模型配置文件 {paths.models_config_file}", "提交的模型配置"
    )
    assert str(submission_error.value) == expected
    assert str(paths.home) not in str(submission_error.value)


def test_organ_settings_override_model_settings(tmp_path: Path) -> None:
    config = load_models_config(write_models_toml(tmp_path, VALID_TOML))
    big, small = config.routes_for("replyer")
    assert big.max_tokens == 300  # 器官上写了，优先
    assert big.temperature == 0.9  # 器官没写，用模型自己的
    assert small.max_tokens == 300
    assert small.temperature is None


def test_provider_defaults_apply_when_fields_are_omitted(tmp_path: Path) -> None:
    config = load_models_config(write_models_toml(tmp_path, VALID_TOML))
    assert config.providers[0].timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert config.providers[0].max_retry == DEFAULT_MAX_RETRY
    assert config.providers[1].max_retry == 0


def test_default_timeout_is_120_seconds(tmp_path: Path) -> None:
    # D135：默认超时 120 秒（取代 D132 的 90 秒）；显式配置的超时继续生效。
    config = load_models_config(write_models_toml(tmp_path, VALID_TOML))
    assert DEFAULT_TIMEOUT_SECONDS == 120.0
    assert config.providers[0].timeout_seconds == 120.0
    assert config.providers[1].timeout_seconds == 5.0


@pytest.mark.parametrize("total", [5, 21, 120])
def test_old_provider_uses_clamped_default_first_token_timeout(tmp_path: Path, total: int) -> None:
    text = VALID_TOML.replace("timeout_seconds = 5", f"timeout_seconds = {total}")
    config = load_models_config(write_models_toml(tmp_path, text))
    provider = config.providers[1]
    assert provider.first_token_timeout_seconds == 30
    assert provider.first_chunk_timeout == min(total, 30)
    assert config.circuit_breaker.model_dump() == {
        "enabled": True,
        "failure_threshold": 3,
        "cooldown_seconds": 60,
    }


@pytest.mark.parametrize("first", [4, 121, -1, 21, 22])
def test_invalid_explicit_first_token_timeout_reports_config_error(
    tmp_path: Path, first: int
) -> None:
    text = VALID_TOML.replace(
        "timeout_seconds = 5", f"timeout_seconds = 21\nfirst_token_timeout_seconds = {first}"
    )
    assert "first_token_timeout_seconds" in load_error(tmp_path, text)


def test_timeout_and_circuit_breaker_can_be_disabled(tmp_path: Path) -> None:
    text = VALID_TOML.replace(
        "timeout_seconds = 5", "timeout_seconds = 5\nfirst_token_timeout_seconds = 0"
    )
    text += "\n[circuit_breaker]\nenabled = false\n"
    config = load_models_config(write_models_toml(tmp_path, text))
    assert config.providers[1].first_chunk_timeout is None
    assert not config.circuit_breaker.enabled


@pytest.mark.parametrize(
    "bad_field", ["failure_threshold = 0", "cooldown_seconds = 0", "bogus = 1"]
)
def test_circuit_breaker_config_rejects_invalid_fields(tmp_path: Path, bad_field: str) -> None:
    assert "circuit_breaker" in load_error(
        tmp_path, VALID_TOML + f"\n[circuit_breaker]\n{bad_field}\n"
    )


def test_prices_are_optional(tmp_path: Path) -> None:
    # D37：账本和预算按 token 计，价格不是必填；不写就是“费用未知”。
    text = VALID_TOML.replace("price_in = 4.0\nprice_out = 8.0\n", "")
    assert text != VALID_TOML
    config = load_models_config(write_models_toml(tmp_path, text))
    big = config.routes_for("replyer")[0].model
    assert (big.price_in, big.price_out, big.price_cache_in) == (None, None, None)


def test_missing_file_explains_how_to_create_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"models\.example\.toml"):
        load_models_config(AureusPaths(tmp_path))


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    assert "不是合法的 TOML" in load_error(tmp_path, "not = [valid")


def test_unknown_organ_is_reported(tmp_path: Path) -> None:
    config = load_models_config(write_models_toml(tmp_path, VALID_TOML))
    with pytest.raises(ConfigError, match="没有器官 planner"):
        config.routes_for("planner")


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("provider = 'local'", "provider = 'nowhere'", "不存在的服务商 nowhere"),
        ("name = 'small'", "name = 'big'", "名字重复"),
        ("price_out = 8.0", "price_out = -1", "price_out"),
        ("max_tokens = 300", "strategy = 'random'\nmax_tokens = 300", "strategy"),
        (
            "max_tokens = 300",
            "reasoning_effort = 'ultra'\nmax_tokens = 300",
            "reasoning_effort",
        ),
        (
            "identifier = 'big-model'",
            "identifier = 'big-model'\nprice_cash = 1",
            "不认识这个字段",
        ),
        (
            "config_version = 1",
            f"config_version = {MODELS_CONFIG_FORMAT_VERSION + 1}",
            "请先升级软件",
        ),
    ],
)
def test_bad_configs_are_rejected_with_a_chinese_reason(
    tmp_path: Path, old: str, new: str, expected: str
) -> None:
    assert old in VALID_TOML
    assert expected in load_error(tmp_path, VALID_TOML.replace(old, new))


def test_pasted_secret_is_rejected_without_echoing_it(tmp_path: Path) -> None:
    pasted = "sk-real-secret-value"
    message = load_error(tmp_path, VALID_TOML.replace("AUREUS_KEY_CLOUD", pasted))
    assert "api_key_env" in message
    assert pasted not in message


def with_periods(*periods: str) -> str:
    """把若干 [[models.price_periods]] 表插进 big 模型，保持后面的 small 表不动。"""
    blocks = "".join(f"\n[[models.price_periods]]\n{period}\n" for period in periods)
    return VALID_TOML.replace("max_tokens = 2000", "max_tokens = 2000" + blocks)


def period(start: str, end: str, price_in: float = 1.0) -> str:
    return (
        f"start = '{start}'\nend = '{end}'\n"
        f"price_in = {price_in}\nprice_out = 2.0\nprice_cache_in = 0.5"
    )


def test_new_model_fields_have_defaults_for_old_configs(tmp_path: Path) -> None:
    config = load_models_config(write_models_toml(tmp_path, VALID_TOML))
    big = config.models[0]
    assert big.enabled is True
    assert big.price_periods == ()
    assert big.thinking is False
    assert big.web_search is False
    assert big.extra_params == {}


def test_price_periods_are_parsed_and_carried_to_routes(tmp_path: Path) -> None:
    config = load_models_config(write_models_toml(tmp_path, with_periods(period("08:00", "12:00"))))
    period_config = config.routes_for("replyer")[0].model.price_periods[0]
    assert (period_config.start, period_config.end) == ("08:00", "12:00")
    assert (
        period_config.price_in,
        period_config.price_out,
        period_config.price_cache_in,
    ) == (1.0, 2.0, 0.5)
    assert (period_config.start_minutes, period_config.end_minutes) == (480, 720)


@pytest.mark.parametrize(
    ("periods", "expected"),
    [
        ((period("08:00", "12:00"), period("11:00", "13:00")), "不能重叠"),
        ((period("10:00", "12:00"), period("08:00", "09:00")), "按开始时间排列"),
        ((period("09:00", "09:00"),), "开始时间必须早于结束时间"),
        ((period("10:00", "09:00"),), "开始时间必须早于结束时间"),
        ((period("8:00", "09:00"),), "price_periods"),
    ],
)
def test_bad_price_periods_are_rejected(
    tmp_path: Path, periods: tuple[str, ...], expected: str
) -> None:
    message = load_error(tmp_path, with_periods(*periods))
    assert expected in message


def test_more_than_twelve_price_periods_are_rejected(tmp_path: Path) -> None:
    periods = tuple(period(f"{hour:02d}:00", f"{hour + 1:02d}:00") for hour in range(13))
    message = load_error(tmp_path, with_periods(*periods))
    assert "分时价格最多 12 段" in message


def test_extra_params_accept_nested_json_values(tmp_path: Path) -> None:
    text = VALID_TOML.replace(
        "max_tokens = 2000",
        "max_tokens = 2000\n"
        "extra_params = { top_p = 0.9, stop = ['a', 'b'], nested = { depth = 2 }, flag = true }",
    )
    config = load_models_config(write_models_toml(tmp_path, text))
    assert config.models[0].extra_params == {
        "top_p": 0.9,
        "stop": ["a", "b"],
        "nested": {"depth": 2},
        "flag": True,
    }


@pytest.mark.parametrize("key", ["model", "messages", "tools", "stream"])
def test_extra_params_cannot_override_core_request_keys(tmp_path: Path, key: str) -> None:
    text = VALID_TOML.replace(
        "max_tokens = 2000",
        f"max_tokens = 2000\nextra_params = {{ {key} = 'x' }}",
    )
    message = load_error(tmp_path, text)
    assert "核心请求字段" in message
    assert key in message


def test_extra_params_must_be_json_representable(tmp_path: Path) -> None:
    text = VALID_TOML.replace(
        "max_tokens = 2000",
        "max_tokens = 2000\nextra_params = { when = 2026-01-01T00:00:00Z }",
    )
    assert "JSON 能表示的类型" in load_error(tmp_path, text)


def test_extra_params_size_is_limited(tmp_path: Path) -> None:
    text = VALID_TOML.replace(
        "max_tokens = 2000",
        f"max_tokens = 2000\nextra_params = {{ big = '{'x' * 9000}' }}",
    )
    assert "最多 8192 字节" in load_error(tmp_path, text)


def with_image_model(value: str, kind: str = "image_embedding") -> str:
    """在 big/small 之后、器官表之前插入一个带图片输入格式的模型。"""
    block = (
        "\n[[models]]\n"
        "name = 'picture'\n"
        "identifier = 'chinese-clip'\n"
        "provider = 'cloud'\n"
        f"kind = '{kind}'\n"
        "\n[models.extra_params]\n"
        f"image_embedding_input = '{value}'\n"
    )
    return VALID_TOML.replace("\n[organs.replyer]", block + "\n[organs.replyer]")


def test_image_embedding_input_accepts_data_uri_on_image_models(tmp_path: Path) -> None:
    # D150：图片嵌入模型可以声明按 data URI 字符串发送。
    config = load_models_config(write_models_toml(tmp_path, with_image_model("{data_uri}")))

    picture = config.models[2]
    assert picture.kind == "image_embedding"
    assert picture.extra_params == {"image_embedding_input": "{data_uri}"}


@pytest.mark.parametrize("value", ["{base64}", "data_uri", ""])
def test_image_embedding_input_rejects_other_values(tmp_path: Path, value: str) -> None:
    message = load_error(tmp_path, with_image_model(value))

    assert "image_embedding_input" in message
    assert "只支持" in message


@pytest.mark.parametrize("kind", ["llm", "embedding"])
def test_image_embedding_input_rejects_other_model_kinds(tmp_path: Path, kind: str) -> None:
    message = load_error(tmp_path, with_image_model("{data_uri}", kind=kind))

    assert "image_embedding_input" in message
    assert "image_embedding" in message


def test_disabled_models_skip_routes_and_reasoning_checks(tmp_path: Path) -> None:
    text = (
        VALID_TOML.replace("max_tokens = 2000", "max_tokens = 2000\nreasoning_levels = ['high']")
        .replace("name = 'small'\nidentifier", "name = 'small'\nenabled = false\nidentifier")
        .replace("max_tokens = 300", "max_tokens = 300\nreasoning_effort = 'high'")
    )
    config = load_models_config(write_models_toml(tmp_path, text))
    assert config.models[1].enabled is False
    assert [route.model.name for route in config.routes_for("replyer")] == ["big"]


def test_disabling_every_bound_model_leaves_no_routes(tmp_path: Path) -> None:
    text = VALID_TOML.replace(
        "name = 'big'\nidentifier", "name = 'big'\nenabled = false\nidentifier"
    ).replace("name = 'small'\nidentifier", "name = 'small'\nenabled = false\nidentifier")
    config = load_models_config(write_models_toml(tmp_path, text))
    assert config.routes_for("replyer") == ()
