"""公共地基 common：配置读取。

运行配置在数据根目录下的 config/aureus.toml，不含密钥；文件不存在就全部用默认值。
密钥不归这里管：它们在加密的密钥库里，由 organs/api_keys.py 读取（D36，D52 第 3 条）。

配置缺字段、多字段或格式不对时，报错是中文，指出是哪个文件的哪个字段，
但不回显填进去的值：万一填错的是密钥，也不会被打印出来。
软件升级时新增的字段必须有默认值，旧配置文件照样能读（见 docs/14-user-data.md 第 3 节）。
"""

import tomllib
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aureus.common.budget_calendar import DEFAULT_DAY_START_HOUR, BudgetCalendar, load_timezone
from aureus.common.paths import AureusPaths

# 当前软件认识的配置格式版本。以后改了配置格式就加一，并写迁移说明。
CONFIG_FORMAT_VERSION = 1

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
    # 每拍是否让时间推进生物模型（激素随时间衰减）。0.9a 修好模型后默认开
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


class BodyConfig(BaseModel):
    """身体信号的可调参数（aureus.toml 的 [body]，设计见 04 号文档、D53）。

    每个信号都有自己的开关（D5）；社交需求在 Phase 2 和主动社交一起做（D53）。
    """

    # 严格类型，理由同 SafetyConfig。allow_inf_nan=False：TOML 能写 nan 和 inf。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    # 精力信号的总开关（D5：每个信号都能单独关）。关掉后不再按每日额度限制调用，
    # 精力记为「不限」；月度 token 上限仍然生效，那是安全检查，不归身体管。
    energy_enabled: bool = True
    # 每日额度里只留给回应用户的比例（04 号文档第 4 节）：可选的调用用到
    # 额度的 (1 - 这个比例) 就停，剩下的只给用户。
    user_reserve_fraction: float = Field(default=0.15, ge=0, lt=1)
    # 档位分界，按「精力 = 今日剩余额度 ÷ 今日额度」算；低于第一个数进节省档，
    # 低于第二个进低电量，低于第三个进透支保护（04 号文档第 4 节）。
    saving_below: float = Field(default=0.5, gt=0, le=1)
    low_below: float = Field(default=0.2, gt=0, le=1)
    overdraft_below: float = Field(default=0.05, ge=0, le=1)

    # 疲劳（04 号文档第 5 节，D57）：来自最近一段时间的器官调用次数。
    fatigue_enabled: bool = True
    # 看最近多少分钟的调用。
    fatigue_window_minutes: int = Field(default=30, ge=1)
    # 窗口里调用满这么多次，疲劳就是 100%。
    fatigue_full_calls: int = Field(default=60, ge=1)
    # 疲劳达到这个比例算「偏高」。
    fatigue_high_at: float = Field(default=0.7, gt=0, le=1)
    # 偏高时，收到消息后先等这么多秒再开始回，攒几条一起回。
    fatigue_batch_seconds: float = Field(default=20.0, ge=0)

    # 压力（D57）：来自最近的调用失败率和排队的消息数。
    stress_enabled: bool = True
    # 看最近多少次调用；失败次数不足这么多次时按这个数当分母，免得第一次失败就是 100%。
    stress_window_calls: int = Field(default=20, ge=1)
    # 排队的消息数达到这么多，积压带来的压力就是 100%。
    stress_backlog_full: int = Field(default=5, ge=1)
    # 压力达到这个比例算「偏高」，会发一次自检通知。
    stress_high_at: float = Field(default=0.6, gt=0, le=1)

    # 睡眠（D57）：夜间时段，当地时间；开始大于结束表示跨过午夜（比如 23 到 7）。
    sleep_enabled: bool = True
    night_start_hour: int = Field(default=1, ge=0, le=23)
    night_end_hour: int = Field(default=7, ge=0, le=23)
    # 夜里定时拍的间隔；只会比白天慢，不会更快。有新消息时照样马上醒来。
    night_tick_interval_seconds: float = Field(default=300.0, ge=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if not self.overdraft_below < self.low_below < self.saving_below:
            raise ValueError("档位分界要满足 overdraft_below < low_below < saving_below")
        if self.night_start_hour == self.night_end_hour:
            raise ValueError(
                "夜间时段的开始和结束不能是同一个钟点；不想要夜间时段就关掉 sleep_enabled"
            )
        return self


class TimeConfig(BaseModel):
    """「一天」和「一个月」怎么划分（aureus.toml 的 [time]，D53 第 4 条）。"""

    # 严格类型，理由同 SafetyConfig。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # IANA 时区名，比如 "Asia/Shanghai"。不写就用这台电脑设置的时区。
    timezone: str | None = Field(default=None, min_length=1)
    # 一天从当地几点开始；每月 1 日的这个时刻算新的一个月。
    day_start_hour: int = Field(default=DEFAULT_DAY_START_HOUR, ge=0, le=23)

    @field_validator("timezone")
    @classmethod
    def _check_timezone_name(cls, value: str | None) -> str | None:
        # 启动时就查：时区名写错了，要在读配置时报错，不要等到算预算时才发现。
        if value is not None:
            load_timezone(value)
        return value

    def build_calendar(self) -> BudgetCalendar:
        """按这份配置建日历。"""
        zone = None if self.timezone is None else load_timezone(self.timezone)
        return BudgetCalendar(zone, self.day_start_hour)


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
    body: BodyConfig = Field(default_factory=BodyConfig)
    time: TimeConfig = Field(default_factory=TimeConfig)
    snapshots: SnapshotsConfig = Field(default_factory=SnapshotsConfig)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        # 允许写小写（debug），统一转成大写再检查。
        return value.upper() if isinstance(value, str) else value


@dataclass(frozen=True)
class Settings:
    """一次启动用到的全部配置：路径和运行配置。"""

    paths: AureusPaths
    runtime: RuntimeConfig


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


def load_settings(paths: AureusPaths) -> Settings:
    """读取全部配置。

    Args:
        paths: 数据根目录的路径集合。

    Returns:
        路径和运行配置。

    Raises:
        ConfigError: 运行配置有问题。
    """
    return Settings(paths=paths, runtime=load_runtime_config(paths))
