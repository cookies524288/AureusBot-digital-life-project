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
    # 已废弃（D69 第 7 条）：旧的四种神经递质模型已删，这个开关不再起作用。
    # 写了它的旧配置照常启动，启动时警告一次；下个阶段删掉这个字段。None 表示没写。
    advance_biomodel: bool | None = None
    # 每拍算出的认知状态是否在自述变化时写进时间线（D34、D41 第 4 条；D5 每个机制都能单独关）。
    broadcast_cognitive_state: bool = True
    # 关闭时最多等进行中的器官调用多少秒，等不到就取消（D41 第 6 条）。
    shutdown_grace_seconds: float = Field(default=60.0, ge=0)
    # 回复时带上最近多少条对话（包括刚收到的新消息）。
    recent_message_limit: int = Field(default=20, ge=1)
    # 回复用哪个器官；名字要和 models.toml 的 [organs.名字] 一致。
    reply_organ: str = Field(default="replyer", min_length=1)
    # 规划器（路线图 1.6，D59）：正常档先由它决定回不回、说什么、多长，再交给回复器。
    # 关掉后每次都合并成一次调用（D5：每个机制都能单独关）。
    planning_enabled: bool = True
    # 规划用哪个器官；planning_enabled 打开时，models.toml 里必须有它。
    plan_organ: str = Field(default="planner", min_length=1)
    # 节省档、低电量（含透支保护）时带上最近多少条对话；都不会超过 recent_message_limit。
    saving_message_limit: int = Field(default=10, ge=1)
    low_message_limit: int = Field(default=5, ge=1)
    # 说话方式（路线图 1.7，D60）：把一段回复拆成几条短消息发出。关掉后整段一条发出。
    split_reply_enabled: bool = True
    # 一段回复最多拆成几条。
    max_reply_messages: int = Field(default=4, ge=1)
    # 一条至少多少个字；更短的句子并进相邻的一句。
    min_message_chars: int = Field(default=4, ge=1)
    # 用户在 identity.toml 里改了设定后，这次运行的系统上下文里告诉它哪几层被改了（D62）。
    # 只说哪几层，不给旧内容；时间线照样记，关掉只影响它知不知道。
    identity_changes_in_prompt: bool = True
    # 扮演模式（路线图 1.12，D65）：实验对照组，默认关闭。开启时 open_core 不创建身体和样本存储，
    # 所有身体信号机制都不起作用，系统上下文里只写一段固定的「你有精力和心情」让模型自己演。
    # 月度上限、失控拦截、急停、权限检查这些安全检查照常生效。
    roleplay_mode: bool = False


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
    # 是否把身体状态摘要放进给大模型的系统上下文（04 号文档第 6 节，D58）。
    # 关掉后它不知道自己的身体状态，被问起时没有事实可说；行为影响不受这个开关管。
    state_in_prompt_enabled: bool = True
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

    # 身体信号样本（D64）：心跳每隔一段时间把当时的读数记一行，观察台画曲线靠它。
    # 观察台是独立进程、只读数据库，看不到心跳内存，所以读数要先进数据库。
    # 总开关关掉后不写样本，观察台显示「没有样本」。
    sample_enabled: bool = True
    # 两次记样本之间隔多少秒。
    sample_interval_seconds: float = Field(default=60.0, ge=1)
    # 样本留多少天，启动时清掉更早的行。样本只是读数、不是经历，
    # 所以不受时间线只追加的规则约束（D64 第 2 条）。
    sample_retention_days: int = Field(default=30, ge=1)

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


class WebConfig(BaseModel):
    """网页通道的配置（aureus.toml 的 [web]，设计见 docs/10-decisions.md 的 D63、D53 第 5 条）。"""

    # 严格类型，理由同 SafetyConfig：端口写成字符串要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 网页通道的总开关（D5）；关掉后 aureus chat 不启动网页服务。
    enabled: bool = True
    # 默认只监听本机 127.0.0.1；打开后监听局域网，手机才能用。
    # 局域网里是不加密的 HTTP，同一 Wi-Fi 下有人抓包能看到内容（D53 第 5 条）。
    allow_remote: bool = False
    # 监听端口。默认值是随手挑的、不常被占用的端口，占用了可以改；
    # 不许小于 1024：那一段在不少系统上需要管理员权限。
    port: int = Field(default=8765, ge=1024, le=65535)
    # 登录状态的有效天数，从最后一次活动算起，每次请求续期（D63 第 7 条）。
    session_days: int = Field(default=7, ge=1)
    # 登录失败限流（按 IP）：窗口内失败这么多次，就在锁定期内拒绝这个 IP 的登录（D63 第 7 条）。
    login_max_failures: int = Field(default=5, ge=1)
    login_window_seconds: int = Field(default=300, ge=1)
    login_lockout_seconds: int = Field(default=900, ge=1)


class FeedbackConfig(BaseModel):
    """反馈的配置（aureus.toml 的 [feedback]，设计见 docs/10-decisions.md 的 D67）。"""

    # 严格类型，理由同 SafetyConfig：窗口写成字符串或小数要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 反馈的总开关（D5）；关掉后不记预期和结果，行为不受影响。
    enabled: bool = True
    # 自信取每个类别最近多少次有结果的行动（结果未知的不计入）。
    window_size: int = Field(default=50, ge=1)
    # 有结果的次数少于这么多时，自信与先验 0.5 按样本数加权混合，并标「样本不足」。
    min_samples: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.min_samples > self.window_size:
            raise ValueError("min_samples 不能大于 window_size，否则样本永远不够")
        return self


class JudgeConfig(BaseModel):
    """判断器官的配置（aureus.toml 的 [judge]，设计见 docs/10-decisions.md 的 D68）。"""

    # 严格类型，理由同 SafetyConfig：开关写成数字、条数写成字符串要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 判断的总开关（D5）。默认开：2.3 激素已经读判断结果（D68 第 7 条）；
    # 没配 judge 器官时会跳过判断并只记一次日志，不报错。
    enabled: bool = True
    # models.toml 里的器官名；没配这个器官时跳过判断，不报错（D68 第 7 条）。
    organ: str = Field(default="judge", min_length=1)
    # 判断最多等多久。判断只是回复前的参考，等太久不如直接回复，所以给一个不长的上限；
    # 10 秒是随手挑的，没有实测依据。
    timeout_seconds: float = Field(default=10.0, gt=0)
    # 除了这次要回复的消息，再带之前几条对话；0 表示只看这一条。
    context_messages: int = Field(default=5, ge=0)


class HormonesConfig(BaseModel):
    """激素的配置（aureus.toml 的 [hormones]，设计见 docs/10-decisions.md 的 D69）。

    下面的数值都是随手挑的初值，没有实测依据，跑起来之后按表现调整。
    """

    # 严格类型，理由同 SafetyConfig：阈值写成字符串、开关写成数字要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 激素的总开关（D5）；关掉后不触发、不进状态摘要、不影响行为。
    enabled: bool = True
    # 四种激素各自的开关（D5）；关掉的那种不触发、不进摘要、不影响行为。
    anger_enabled: bool = True
    joy_enabled: bool = True
    hurt_enabled: bool = True
    surprise_enabled: bool = True
    # 半衰期（秒）：浓度降到一半要多久（03 号文档）。
    anger_half_life_seconds: float = Field(default=600.0, gt=0)
    joy_half_life_seconds: float = Field(default=900.0, gt=0)
    hurt_half_life_seconds: float = Field(default=1800.0, gt=0)
    surprise_half_life_seconds: float = Field(default=180.0, gt=0)
    # 判断器官报的事：上涨量 = 基础量 × 强度 × 把握，单次不超过上限（D69 第 2 条）。
    base_amount: float = Field(default=0.5, gt=0, le=1)
    max_single_rise: float = Field(default=0.4, gt=0, le=1)
    # 把握低于这个值的判断不算，免得拿不准的猜测也让它动气。
    min_confidence: float = Field(default=0.3, ge=0, le=1)
    # 同一个人在窗口内反复触发，第 n 次乘以这个系数的 n − 1 次方，刷屏刷不出情绪。
    repeat_decay: float = Field(default=0.6, gt=0, le=1)
    repeat_window_seconds: float = Field(default=600.0, gt=0)
    # 对某个人的怒气达到回击线，允许回击；回落到平息线以下，一场冲突结束；
    # 达到冷处理线，规划器可以选择不回（D69 第 3 条）。要满足 平息 < 回击 < 冷处理。
    calm_below: float = Field(default=0.2, ge=0, le=1)
    retaliate_at: float = Field(default=0.5, gt=0, le=1)
    cold_shoulder_at: float = Field(default=0.8, gt=0, le=1)
    # 同一场冲突里最多回击几次；到了就改成冷静回应。
    max_retaliations: int = Field(default=3, ge=0)
    # 低落达到这个值，这次回复的长度上限降一档。
    hurt_shorten_at: float = Field(default=0.5, gt=0, le=1)
    # 惊讶达到这个值，这一拍不等疲劳攒消息。
    surprise_at: float = Field(default=0.5, gt=0, le=1)
    # 反馈里结果和预期把握差这么多，才算「差得很多」，触发惊讶。
    surprise_gap: float = Field(default=0.5, gt=0, le=1)

    @model_validator(mode="after")
    def _check_conflict_levels(self) -> Self:
        if not self.calm_below < self.retaliate_at < self.cold_shoulder_at:
            raise ValueError("要满足 calm_below < retaliate_at < cold_shoulder_at")
        return self


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
    web: WebConfig = Field(default_factory=WebConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    hormones: HormonesConfig = Field(default_factory=HormonesConfig)

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
