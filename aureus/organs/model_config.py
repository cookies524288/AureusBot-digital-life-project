"""器官层：模型配置（服务商、模型、器官三层）。

设计见 docs/15-model-management.md（D30）。config/models.toml 不含密钥，
服务商里只写密钥的名字，密钥本身放在加密的密钥库里（D36）。
结构参考 MaiBot 的模型配置（GPL-3.0，只参考结构，不复制代码）。
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aureus.common.config import ConfigError
from aureus.common.paths import AureusPaths

# 当前软件认识的模型配置格式版本。以后改了格式就加一，并写迁移说明。
MODELS_CONFIG_FORMAT_VERSION = 1

# 项目自带的器官；网页不能删除，模型可以暂时留空（D86）。
BUILTIN_ORGANS = ("replyer", "planner", "judge", "summarizer")

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
# litellm 1.109.1 的 reasoning_effort 取值（D84）。
ReasoningLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "default"]


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
    # 没列等级表示模型不声明支持思考等级（D84）。
    reasoning_levels: tuple[ReasoningLevel, ...] = ()


class OrganConfig(BaseModel):
    """器官：这个器官按顺序用哪些模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 按名字引用模型，排在前面的先用。
    models: tuple[str, ...] = ()
    strategy: OrganStrategy = "sequential"
    # 写在器官上的参数优先于模型自己的参数。
    temperature: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)
    # 配置了时，器官引用的每个模型都必须声明支持这档等级（D84）。
    reasoning_effort: ReasoningLevel | None = None


@dataclass(frozen=True)
class ModelRoute:
    """器官的一次调用可以走的一条路：模型、它的服务商，以及最终生效的参数。"""

    model: ModelConfig
    provider: ProviderConfig
    temperature: float | None
    max_tokens: int | None
    reasoning_effort: ReasoningLevel | None = None


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
        # 模型删除后保留器官引用并给警告；其余配置错误仍拒绝（D86）。
        provider_names = _require_unique("服务商", [item.name for item in self.providers])
        model_names = _require_unique("模型", [item.name for item in self.models])
        models_by_name = {item.name: item for item in self.models}
        for model in self.models:
            if model.provider not in provider_names:
                raise ValueError(f"模型 {model.name} 引用了不存在的服务商 {model.provider}")
        for organ_id, organ in self.organs.items():
            for model_name in organ.models:
                if model_name not in model_names:
                    continue
                if (
                    organ.reasoning_effort is not None
                    and organ.reasoning_effort not in models_by_name[model_name].reasoning_levels
                ):
                    raise ValueError(
                        f"器官 {organ_id} 的思考等级 {organ.reasoning_effort} 不是模型 "
                        f"{model_name} 支持的（没写 reasoning_levels 就是不支持）"
                    )
        return self

    def warnings(self) -> list[str]:
        """列出空器官和失效引用，供网页提示；不修改用户的引用列表。"""
        names = {model.name for model in self.models}
        result: list[str] = []
        for name, organ in self.organs.items():
            if not organ.models:
                result.append(f"{name}未配置模型")
            missing = [model for model in organ.models if model not in names]
            if missing:
                result.append(f"{name}引用了不存在的模型：{'、'.join(missing)}")
        return result

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
            if model_name not in models:
                continue
            model = models[model_name]
            routes.append(
                ModelRoute(
                    model=model,
                    provider=providers[model.provider],
                    temperature=_first_set(organ.temperature, model.temperature),
                    max_tokens=_first_set(organ.max_tokens, model.max_tokens),
                    reasoning_effort=organ.reasoning_effort,
                )
            )
        return tuple(routes)


def _describe_errors(source: str, error: ValidationError) -> str:
    """把校验错误改写成中文：只保留字段位置和原因，不带填进去的值。"""
    lines = [f"{source} 有问题："]
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


def validate_models_config(raw: Mapping[str, Any], source: str) -> ModelsConfig:
    """校验模型配置数据，错误文字由调用方指定来源。

    Args:
        raw: 已解析的 TOML 配置。
        source: 错误消息中的来源说明，不应包含敏感路径。

    Returns:
        校验通过的模型配置。

    Raises:
        ConfigError: 配置字段、模型引用或格式版本不符合要求。
    """
    try:
        config = ModelsConfig.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(_describe_errors(source, error)) from None
    if config.config_version > MODELS_CONFIG_FORMAT_VERSION:
        raise ConfigError(
            f"{source} 的 config_version 是 {config.config_version}，"
            f"比当前软件认识的版本 {MODELS_CONFIG_FORMAT_VERSION} 更新。请先升级软件。"
        )
    return config


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
    # 配置比软件新（降级）时拒绝读取，而不是硬读：见 docs/14-user-data.md 第 3 节。
    return validate_models_config(raw, f"模型配置文件 {path}")
