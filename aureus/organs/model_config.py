"""器官层：模型配置（服务商、模型、器官三层）。

设计见 docs/15-model-management.md（D30）。config/models.toml 不含密钥，
服务商里只写密钥的名字，密钥本身放在加密的密钥库里（D36）。
结构参考 MaiBot 的模型配置（GPL-3.0，只参考结构，不复制代码）。
"""

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aureus.common.config import ConfigError
from aureus.common.paths import AureusPaths
from aureus.common.vault import ENTRY_ID_PATTERN
from aureus.organs.topology import BUILTIN_ORGANS as BUILTIN_ORGANS

# 当前软件认识的模型配置格式版本。以后改了格式就加一，并写迁移说明。
MODELS_CONFIG_FORMAT_VERSION = 1

# 服务商没写超时和重试次数时的默认值。超时 120 秒（D135，取代 D132 的 90 秒）。
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RETRY = 2
DEFAULT_FIRST_TOKEN_TIMEOUT_SECONDS = 30.0
FIRST_TOKEN_TIMEOUT_RANGE = (5.0, 120.0)

# 分时价格最多写这么多段（D134）：一天 24 小时按分钟切，12 段已经够用，
# 再多的时段在网页上也看不清，多半是填错了。
MAX_PRICE_PERIODS = 12
# 额外参数是给个别服务商的补充设置，不是放整份请求内容的地方（D134）。
# 8 KB 够放常见的 top_p、stop 之类，也能挡住误粘贴的大段文字。
MAX_EXTRA_PARAMS_BYTES = 8192

# 这些键决定请求发到哪个模型、发什么内容，额外参数不能覆盖（D134）。
# 其余显式字段（温度、最大 token、思考……）只是优先级更高：额外参数不冲突时仍可补上。
EXTRA_PARAMS_CORE_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "stream",
        "stream_options",
        "timeout",
        "num_retries",
        "api_base",
        "api_key",
    }
)

# 分时价格用当地时间的 “时:分”。结束时间额外允许 24:00，表示当天结束（左闭右开）。
_CLOCK_TIME_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
_CLOCK_END_TIME_PATTERN = r"^(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)$"

# 密钥的环境变量名必须以 AUREUS_KEY_ 开头：不会和别的程序的变量混在一起，
# 也能挡住“把密钥本身粘贴进配置文件”的失误（真密钥不符合这个格式，会当场报错）。
API_KEY_ENV_PATTERN = r"^AUREUS_KEY_[A-Z0-9_]+$"

# D24 规定的四种协议。
ProviderProtocol = Literal["openai", "openai_responses", "anthropic", "gemini"]
ModelKind = Literal["llm", "embedding", "image_embedding", "transcription"]
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
    # 网页按名称选择；保存稳定 id，改名不影响引用（D87）。
    api_key_ref: str | None = Field(default=None, pattern=ENTRY_ID_PATTERN)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    first_token_timeout_seconds: float = DEFAULT_FIRST_TOKEN_TIMEOUT_SECONDS
    max_retry: int = Field(default=DEFAULT_MAX_RETRY, ge=0)

    @field_validator("first_token_timeout_seconds")
    @classmethod
    def _check_first_token_range(cls, value: float) -> float:
        minimum, maximum = FIRST_TOKEN_TIMEOUT_RANGE
        if value != 0 and not minimum <= value <= maximum:
            raise ValueError("首包超时必须为 0 或 5～120 秒。")
        return value

    @model_validator(mode="after")
    def _one_key_source(self) -> Self:
        if self.api_key_env is not None and self.api_key_ref is not None:
            raise ValueError("服务商只能选择一个密钥来源。")
        return self

    @model_validator(mode="after")
    def _check_explicit_first_token_timeout(self) -> Self:
        if (
            "first_token_timeout_seconds" in self.model_fields_set
            and self.first_token_timeout_seconds != 0
            and self.first_token_timeout_seconds >= self.timeout_seconds
        ):
            raise ValueError("first_token_timeout_seconds 必须小于服务商的 timeout_seconds。")
        return self

    @property
    def first_chunk_timeout(self) -> float | None:
        """普通流式请求的生效首包时限；旧配置缺字段时仍以总时限为上限。"""
        if self.first_token_timeout_seconds == 0:
            return None
        return min(self.timeout_seconds, self.first_token_timeout_seconds)


class CircuitBreakerConfig(BaseModel):
    """按服务商保护业务调用；手动测速仍按原有规则运行（D148）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    failure_threshold: int = Field(default=3, ge=1)
    cooldown_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)


class PricePeriod(BaseModel):
    """模型的分时价格：当地时间的某一段，价格单位与默认价格相同（D134）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 当地时间 “时:分”，左闭右开：08:00–12:00 包含 08:00，不包含 12:00。
    start: str = Field(pattern=_CLOCK_TIME_PATTERN)
    end: str = Field(pattern=_CLOCK_END_TIME_PATTERN)
    price_in: float = Field(ge=0)
    price_out: float = Field(ge=0)
    price_cache_in: float = Field(ge=0)

    @model_validator(mode="after")
    def _start_before_end(self) -> Self:
        if self.start_minutes >= self.end_minutes:
            raise ValueError("分时价格的开始时间必须早于结束时间。")
        return self

    @property
    def start_minutes(self) -> int:
        """开始时刻在当天的第几分钟（0 到 1439）。"""
        return _minutes_since_midnight(self.start)

    @property
    def end_minutes(self) -> int:
        """结束时刻在当天的第几分钟（0 到 1440，24:00 表示当天结束）。"""
        return _minutes_since_midnight(self.end)


def _minutes_since_midnight(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


class ModelConfig(BaseModel):
    """模型：服务商提供的某个具体模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 自己起的名字，器官按它引用模型。
    name: str = Field(min_length=1)
    # 服务商给的模型标识。
    identifier: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    kind: ModelKind = "llm"
    vision: bool = False
    # 停用的模型仍保留器官绑定，只是不进入路由（D134）；重新启用就恢复。
    enabled: bool = True
    # 价格单位：元 / 百万 token，可选（D37）。账本和预算按 token 计，不靠价格运作；
    # 填了价格只用来显示估算费用。不写就是“费用未知”，不会被当成免费。
    price_in: float | None = Field(default=None, ge=0)
    price_out: float | None = Field(default=None, ge=0)
    # 命中缓存的输入价格。不写就按 price_in 算：宁可高估花费，也不低估。
    price_cache_in: float | None = Field(default=None, ge=0)
    # 分时价格：命中时段时整段替换默认价格，未命中仍用默认价格（D134）。
    price_periods: tuple[PricePeriod, ...] = ()
    temperature: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)
    # 没列等级表示模型不声明支持思考等级（D84）。
    reasoning_levels: tuple[ReasoningLevel, ...] = ()
    # 为 true 时请求里写 thinking: {"type": "enabled"}（D134）。
    thinking: bool = False
    # 为 true 时给支持原生联网搜索的协议加搜索工具（D134）。
    web_search: bool = False
    # 合并进请求参数的补充设置；不能覆盖核心请求字段（D134）。
    extra_params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_options(cls, data: Any) -> Any:
        if isinstance(data, dict) and "tool_calls" in data:
            # 旧版保存过工具调用开关；现在每个 LLM 都会测试工具调用。
            data = {key: value for key, value in data.items() if key != "tool_calls"}
        if isinstance(data, dict) and data.get("kind") == "vision":
            return {**data, "kind": "llm", "vision": True}
        return data

    @model_validator(mode="after")
    def _validate_vision(self) -> Self:
        if self.vision and self.kind != "llm":
            raise ValueError("只有 llm 模型可以开启视觉。")
        return self

    @model_validator(mode="after")
    def _validate_price_periods(self) -> Self:
        if len(self.price_periods) > MAX_PRICE_PERIODS:
            raise ValueError(f"分时价格最多 {MAX_PRICE_PERIODS} 段。")
        previous_end = 0
        for period in self.price_periods:
            if period.start_minutes < previous_end:
                raise ValueError("分时价格要按开始时间排列，而且不能重叠。")
            previous_end = period.end_minutes
        return self

    @model_validator(mode="after")
    def _validate_extra_params(self) -> Self:
        core = sorted(EXTRA_PARAMS_CORE_KEYS.intersection(self.extra_params))
        if core:
            raise ValueError(f"额外参数不能覆盖核心请求字段：{'、'.join(core)}")
        try:
            encoded = json.dumps(self.extra_params, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError(
                "额外参数的值必须是 JSON 能表示的类型（文字、数字、开关、对象或数组）。"
            ) from None
        if len(encoded.encode("utf-8")) > MAX_EXTRA_PARAMS_BYTES:
            raise ValueError(f"额外参数最多 {MAX_EXTRA_PARAMS_BYTES} 字节。")
        return self


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
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

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
                model = models_by_name[model_name]
                # 停用的模型不进入路由，思考等级也不用为它校验（D134）。
                if not model.enabled:
                    continue
                if (
                    organ.reasoning_effort is not None
                    and organ.reasoning_effort not in model.reasoning_levels
                ):
                    raise ValueError(
                        f"器官 {organ_id} 的思考等级 {organ.reasoning_effort} 不是模型 "
                        f"{model_name} 支持的（没写 reasoning_levels 就是不支持）"
                    )
        return self

    def configured_text_organs(self) -> frozenset[str]:
        """有对话模型路线的器官名，用于显示当前文本任务分配。"""
        return frozenset(
            name
            for name in self.organs
            if any(route.model.kind == "llm" for route in self.routes_for(name))
        )

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
            # 停用的模型保留绑定，只是不参与路由（D134）。
            if not model.enabled:
                continue
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
