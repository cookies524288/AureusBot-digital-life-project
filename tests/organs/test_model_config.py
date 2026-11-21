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
