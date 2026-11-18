"""器官层：模型配置（服务商、模型、器官三层）。

设计见 docs/15-model-management.md（D30）。config/models.toml 不含密钥，
服务商里只写密钥的名字，密钥本身放在加密的密钥库里（D36）。
结构参考 MaiBot 的模型配置（GPL-3.0，只参考结构，不复制代码）。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aureus.common.config import ConfigError
from aureus.common.paths import AureusPaths

# 当前软件认识的模型配置格式版本。以后改了格式就加一，并写迁移说明。
MODELS_CONFIG_FORMAT_VERSION = 1

# 服务商没写超时和重试次数时的默认值。
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRY = 2

# 密钥的环境变量名必须以 AUREUS_KEY_ 开头：不会和别的程序的变量混在一起，
# 也能挡住“把密钥本身粘贴进配置文件”的失误（真密钥不符合这个格式，会当场报错）。
API_KEY_ENV_PATTERN = r"^AUREUS_KEY_[A-Z0-9_]+$"

# D24 规定的四种协议。
ProviderProtocol = Literal["openai", "openai_responses", "anthropic", "gemini"]
ModelKind = Literal["llm", "embedding", "vision"]
# 目前只做“按顺序顺延”。D30 里的 balance、random 等用得上时再加（开发守则 2.5）；
# 现在写别的值会直接报错，而不是悄悄当成顺序。
OrganStrategy = Literal["sequential"]


class ProviderConfig(BaseModel):
    """服务商：去哪里调用、怎么认证。"""

    # extra="forbid"：写错字段名时宁可停下来，也不要悄悄忽略。
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 自己起的名字，模型按它引用服务商。
    name: str = Field(min_length=1)
    protocol: ProviderProtocol
    # 不写就用该协议的官方地址。
    base_url: str | None = None
    # 密钥所在的环境变量名，不是密钥本身。不写表示这个服务商不需要密钥（比如本地模型）。
    api_key_env: str | None = Field(default=None, pattern=API_KEY_ENV_PATTERN)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    max_retry: int = Field(default=DEFAULT_MAX_RETRY, ge=0)


class ModelConfig(BaseModel):
    """模型：服务商提供的某个具体模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 自己起的名字，器官按它引用模型。
    name: str = Field(min_length=1)
    # 服务商给的模型标识。
    identifier: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    kind: ModelKind = "llm"
    # 价格单位：元 / 百万 token，可选（D37）。账本和预算按 token 计，不靠价格运作；
    # 填了价格只用来显示估算费用。不写就是“费用未知”，不会被当成免费。
    price_in: float | None = Field(default=None, ge=0)
    price_out: float | None = Field(default=None, ge=0)
    # 命中缓存的输入价格。不写就按 price_in 算：宁可高估花费，也不低估。
    price_cache_in: float | None = Field(default=None, ge=0)
    temperature: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)


class OrganConfig(BaseModel):
    """器官：这个器官按顺序用哪些模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 按名字引用模型，排在前面的先用。
    models: tuple[str, ...] = Field(min_length=1)
    strategy: OrganStrategy = "sequential"
    # 写在器官上的参数优先于模型自己的参数。
    temperature: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class ModelRoute:
    """器官的一次调用可以走的一条路：模型、它的服务商，以及最终生效的参数。"""

    model: ModelConfig
    provider: ProviderConfig
    temperature: float | None
    max_tokens: int | None


def _first_set[T](first: T | None, second: T | None) -> T | None:
    return first if first is not None else second


def _require_unique(kind: str, names: list[str]) -> set[str]:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"{kind}的名字重复了：{name}")
        seen.add(name)
    return seen


class ModelsConfig(BaseModel):
    """模型配置（config/models.toml）的全部内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: int = Field(default=MODELS_CONFIG_FORMAT_VERSION, ge=1)
    providers: tuple[ProviderConfig, ...] = ()
    models: tuple[ModelConfig, ...] = ()
    organs: dict[str, OrganConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        # 引用错了就在启动时报错，而不是等到调用模型时才发现（见 15 号文档第 4 节）。
        provider_names = _require_unique("服务商", [item.name for item in self.providers])
        model_names = _require_unique("模型", [item.name for item in self.models])
        for model in self.models:
            if model.provider not in provider_names:
                raise ValueError(f"模型 {model.name} 引用了不存在的服务商 {model.provider}")
        for organ_id, organ in self.organs.items():
            for model_name in organ.models:
                if model_name not in model_names:
                    raise ValueError(f"器官 {organ_id} 引用了不存在的模型 {model_name}")
        return self

    def routes_for(self, organ_id: str) -> tuple[ModelRoute, ...]:
        """列出某个器官可以走的模型，按配置里的顺序。

        Args:
            organ_id: 器官名，比如 planner、replyer。

        Returns:
            按顺序排列的调用路线；器官上写的参数优先于模型自己的参数。

        Raises:
            ConfigError: 配置里没有这个器官。
        """
        organ = self.organs.get(organ_id)
        if organ is None:
            raise ConfigError(f"模型配置里没有器官 {organ_id}（缺少 [organs.{organ_id}]）。")
        models = {item.name: item for item in self.models}
        providers = {item.name: item for item in self.providers}
        routes: list[ModelRoute] = []
        for model_name in organ.models:
            model = models[model_name]
            routes.append(
                ModelRoute(
                    model=model,
                    provider=providers[model.provider],
                    temperature=_first_set(organ.temperature, model.temperature),
                    max_tokens=_first_set(organ.max_tokens, model.max_tokens),
                )
            )
        return tuple(routes)


def _describe_errors(path: Path, error: ValidationError) -> str:
    """把校验错误改写成中文：只保留字段位置和原因，不带填进去的值。"""
    lines = [f"模型配置文件 {path} 有问题："]
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "（整个文件）"
        if item["type"] == "value_error":
            reason = item["msg"].removeprefix("Value error, ")
        elif item["type"] == "missing":
            reason = "缺少必填字段"
        elif item["type"] == "extra_forbidden":
            reason = "不认识这个字段（是不是拼错了？）"
        else:
            reason = f"格式不对（{item['msg']}）"
        lines.append(f"  - 字段 {location}：{reason}")
    return "\n".join(lines)


def load_models_config(paths: AureusPaths) -> ModelsConfig:
    """读取并校验模型配置 config/models.toml。

    Args:
        paths: 数据根目录的路径集合。

    Returns:
        校验过的模型配置。

    Raises:
        ConfigError: 文件不存在、不是合法的 TOML、字段或引用不对，或者来自更新的软件版本。
    """
    path = paths.models_config_file
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(
            f"找不到模型配置文件 {path}。"
            "请把仓库里的 config/models.example.toml 复制过去，再按需修改。"
        ) from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"模型配置文件 {path} 不是合法的 TOML：{error}") from None
    except OSError as error:
        raise ConfigError(f"读不了模型配置文件 {path}：{error}") from None
    try:
        config = ModelsConfig.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(_describe_errors(path, error)) from None
    if config.config_version > MODELS_CONFIG_FORMAT_VERSION:
        # 配置比软件新（降级）时拒绝读取，而不是硬读：见 docs/14-user-data.md 第 3 节。
        raise ConfigError(
            f"模型配置文件 {path} 的 config_version 是 {config.config_version}，"
            f"比当前软件认识的版本 {MODELS_CONFIG_FORMAT_VERSION} 更新。请先升级软件。"
        )
    return config
