"""公共地基 common：配置读取。

两个来源，各管各的：
- 运行配置：数据根目录下的 config/aureus.toml，不含密钥；文件不存在就全部用默认值。
- 密钥：数据根目录下的 .env 和环境变量（环境变量优先）。密钥用 SecretStr 保存，
  打印出来只显示星号。

配置缺字段、多字段或格式不对时，报错是中文，指出是哪个文件的哪个字段，
但不回显填进去的值：万一填错的是密钥，也不会被打印出来。
软件升级时新增的字段必须有默认值，旧配置文件照样能读（见 docs/14-user-data.md 第 3 节）。
"""

import tomllib
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aureus.common.paths import AureusPaths

# 当前软件认识的配置格式版本。以后改了配置格式就加一，并写迁移说明。
CONFIG_FORMAT_VERSION = 1

# 环境变量的统一前缀，比如 AUREUS_LLM_API_KEY。
ENV_PREFIX = "AUREUS_"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class ConfigError(Exception):
    """配置文件或密钥有问题。消息是中文，说明是哪个文件的哪个字段。"""


class SafetyConfig(BaseModel):
    """安全网关的配置（aureus.toml 的 [safety]，设计见 docs/06-safety.md 第 12 节，D39）。

    默认值在这里集中定义，网关和测试都从这里取，代码里不再出现裸的数字。
    """

    # strict=True：只接受 TOML 里写的真实类型。不开的话，pydantic 会悄悄把 true 当成 1、
    # 把 "20" 当成 20，限额之类的安全设置写错了却照样生效，反而更危险。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 每月最多用多少 token（输入加输出）。None 表示不限；写 0 没有明确含义，直接报错。
    monthly_token_limit: int | None = Field(default=None, gt=0)
    # 失控拦截的总开关（D5：每个内部机制都能单独关闭）。
    runaway_guard_enabled: bool = True
    max_calls_per_minute: int = Field(default=20, ge=1)
    max_consecutive_failures: int = Field(default=5, ge=1)
    failure_pause_seconds: int = Field(default=300, ge=1)


class HeartbeatConfig(BaseModel):
    """心跳的配置（aureus.toml 的 [heartbeat]，设计见 docs/10-decisions.md 的 D41）。"""

    # 和 SafetyConfig 一样用严格类型：写成字符串或 true/false 的数字直接报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 没有新消息时，隔多少真实秒跳一拍（02 号文档第 3 节“例如每分钟一次”）。
    # 不许小于 1 秒：写成 0.001 之类的数字，心跳就会空转、占满一个 CPU 核。
    tick_interval_seconds: float = Field(default=60.0, ge=1)
    # 每拍是否让时间推进生物模型，谈话后是否让社交需求回落。0.9a 修好模型后默认开
    # （D41 第 2 条、D42 第 8 条）；关掉后整个模型冻结，可以用来对照（D5）。
    advance_biomodel: bool = True
    # 每拍算出的认知状态是否在自述变化时写进时间线（D34、D41 第 4 条；D5 每个机制都能单独关）。
    broadcast_cognitive_state: bool = True
    # 关闭时最多等进行中的器官调用多少秒，等不到就取消（D41 第 6 条）。
    shutdown_grace_seconds: float = Field(default=60.0, ge=0)
    # 回复时带上最近多少条对话（包括刚收到的新消息）。
    recent_message_limit: int = Field(default=20, ge=1)
    # 回复用哪个器官；名字要和 models.toml 的 [organs.名字] 一致。
    reply_organ: str = Field(default="replyer", min_length=1)


class BiomodelConfig(BaseModel):
    """生物模型的可调参数（aureus.toml 的 [biomodel]，设计见 D42、D43）。

    字段名和 BiologicalState 的同名字段一一对应，默认值在这里集中定义。
    这些数字没有实测依据，是按“多久该觉得累、觉得闷”估的，项目完成后按实际表现再调。
    """

    # 严格类型，理由同 SafetyConfig。allow_inf_nan=False：TOML 能写 nan 和 inf，
    # 算进模型会把状态弄乱。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    # 精力每小时把离满精力的差距补回多少（17 号文档“休息时每小时恢复 15%”）。
    # 0.15 时，从 0.05 回到“正常”（0.5 以上）约 13 小时；空闲时精力停在 0.55–0.6。
    energy_recovery_rate: float = Field(default=0.15, ge=0)
    # 社交需求每小时涨多少（17 号文档）。0.03 时从默认的 0.3 涨到“想找人聊”（0.6）约 10 小时，
    # 也就是大半天没人说话会想聊天。
    social_timeout_per_hour: float = Field(default=0.03, ge=0)
    # 新奇感每小时涨多少。0.05 时从 0.4 涨到“想尝试新东西”（0.6）约 4 小时。
    novelty_growth_per_hour: float = Field(default=0.05, ge=0)
    # 每回复一次，社交需求降多少。按回复计，一次聊天常有好几条：
    # 0.05 时聊 6 来回约抵消 10 小时的积累，不至于几句话就“宁愿独处”。
    social_relief_per_conversation: float = Field(default=0.05, ge=0, le=1)
    # 每回复一次，新奇感降多少。日常聊天带来的新鲜感有限，真正的新事件另由 on_novel_event 处理。
    novelty_relief_per_conversation: float = Field(default=0.03, ge=0, le=1)


class SnapshotsConfig(BaseModel):
    """快照的保留规则（D46）。"""

    # strict=True：上限写成字符串 "1024" 要报错，不要悄悄转成数字（理由同 SafetyConfig）。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 默认值没有实测依据，是按经验估的：一份导出包通常远小于这个数，留出十几份的余地。
    max_total_megabytes: int = Field(default=1024, ge=1)


class RuntimeConfig(BaseModel):
    """运行配置（config/aureus.toml）。

    第一批只放步骤 0.2 用得到的字段；以后每个子系统要用什么再加，
    新字段必须有默认值（见 docs/11-dev-guide.md 第 2.5 节）。
    """

    # extra="forbid"：不认识的字段直接报错。写错字段名时宁可停下来，
    # 也不要悄悄忽略、让人以为设置生效了。
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: int = Field(default=CONFIG_FORMAT_VERSION, ge=1)
    log_level: LogLevel = "INFO"
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    biomodel: BiomodelConfig = Field(default_factory=BiomodelConfig)
    snapshots: SnapshotsConfig = Field(default_factory=SnapshotsConfig)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        # 允许写小写（debug），统一转成大写再检查。
        return value.upper() if isinstance(value, str) else value


class Secrets(BaseSettings):
    """密钥。来自 .env 文件和环境变量（环境变量优先），变量名统一加 AUREUS_ 前缀。

    新增密钥字段时，类型一律用 SecretStr：它打印出来是星号，
    而且 secret_values() 会自动把它交给日志模块去隐藏。
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file_encoding="utf-8",
        extra="ignore",  # .env 里可以放别的变量，不认识的忽略，不报错
        frozen=True,
    )

    # 模型服务的密钥。步骤 0.5 设计器官、定下支持哪几种协议时，再按需要调整或增加字段。
    llm_api_key: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        # .env.example 里留空的变量（AUREUS_LLM_API_KEY=）视为没有填写。
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def secret_values(self) -> list[str]:
        """列出所有已填写的密钥原文，交给日志模块，让它们不会出现在日志里。

        Returns:
            非空密钥的原文列表。
        """
        values: list[str] = []
        for name in type(self).model_fields:
            item = getattr(self, name)
            if isinstance(item, SecretStr) and item.get_secret_value():
                values.append(item.get_secret_value())
        return values


@dataclass(frozen=True)
class Settings:
    """一次启动用到的全部配置：路径、运行配置、密钥。"""

    paths: AureusPaths
    runtime: RuntimeConfig
    secrets: Secrets


_REASON_BY_ERROR_TYPE = {
    "missing": "缺少必填字段",
    "extra_forbidden": "不认识这个字段（是不是拼错了？）",
}


def _describe_errors(source: str, error: ValidationError) -> str:
    """把校验错误改写成中文：只保留字段名和原因，不带填进去的值。"""
    lines = [f"{source}有问题："]
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        field_name = ".".join(str(part) for part in item["loc"]) or "（整个文件）"
        reason = _REASON_BY_ERROR_TYPE.get(item["type"], f"格式不对（{item['msg']}）")
        lines.append(f"  - 字段 {field_name}：{reason}")
    return "\n".join(lines)


def load_runtime_config(paths: AureusPaths) -> RuntimeConfig:
    """读取运行配置 config/aureus.toml。

    Args:
        paths: 数据根目录的路径集合。

    Returns:
        校验过的配置。文件不存在时，返回全部默认值。

    Raises:
        ConfigError: 文件读不了、不是合法的 TOML、字段不对，或者来自更新的软件版本。
    """
    path = paths.runtime_config_file
    if not path.exists():
        return RuntimeConfig()
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"配置文件 {path} 不是合法的 TOML：{error}") from None
    except OSError as error:
        raise ConfigError(f"读不了配置文件 {path}：{error}") from None
    try:
        config = RuntimeConfig.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(_describe_errors(f"配置文件 {path} ", error)) from None
    if config.config_version > CONFIG_FORMAT_VERSION:
        # 数据或配置比软件新（降级）时拒绝读取，而不是硬读：见 docs/14-user-data.md 第 3 节。
        raise ConfigError(
            f"配置文件 {path} 的 config_version 是 {config.config_version}，"
            f"比当前软件认识的版本 {CONFIG_FORMAT_VERSION} 更新。请先升级软件。"
        )
    return config


def load_secrets(paths: AureusPaths) -> Secrets:
    """读取密钥：数据根目录下的 .env，加上环境变量（环境变量优先）。

    Args:
        paths: 数据根目录的路径集合。

    Returns:
        密钥集合。.env 不存在也可以，对应的密钥就是 None。

    Raises:
        ConfigError: 密钥配置的格式不对。
    """
    try:
        # pyright 误报 _env_file：pydantic-settings 确实接受这个参数，测试里有覆盖。
        return Secrets(_env_file=paths.env_file)  # pyright: ignore[reportCallIssue]
    except ValidationError as error:
        # from None：不带上原始异常，免得追踪信息里带出填进去的值。
        raise ConfigError(_describe_errors("密钥配置（.env 文件或环境变量）", error)) from None


def load_settings(paths: AureusPaths) -> Settings:
    """读取全部配置。

    Args:
        paths: 数据根目录的路径集合。

    Returns:
        路径、运行配置和密钥。

    Raises:
        ConfigError: 任何一部分配置有问题。
    """
    return Settings(
        paths=paths,
        runtime=load_runtime_config(paths),
        secrets=load_secrets(paths),
    )
