"""公共地基 common：配置读取。

运行配置在数据根目录下的 config/aureus.toml，不含密钥；文件不存在就全部用默认值。
密钥不归这里管：它们在加密的密钥库里，由 organs/api_keys.py 读取（D36，D52 第 3 条）。

配置缺字段、多字段或格式不对时，报错是中文，指出是哪个文件的哪个字段，
但不回显填进去的值：万一填错的是密钥，也不会被打印出来。
软件升级时新增的字段必须有默认值，旧配置文件照样能读（见 docs/14-user-data.md 第 3 节）。
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aureus.common.budget_calendar import DEFAULT_DAY_START_HOUR, BudgetCalendar, load_timezone
from aureus.common.paths import AureusPaths

# 当前软件认识的配置格式版本。以后改了配置格式就加一，并写迁移说明。
CONFIG_FORMAT_VERSION = 1

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
DEFAULT_WEB_PORT = 28976
DEFAULT_EMOJI_REGISTRATION_LIMIT = 500


class ConfigError(Exception):
    """配置文件或密钥有问题。消息是中文，说明是哪个文件的哪个字段。"""


def drop_legacy_keys(value: object, legacy_keys: tuple[str, ...]) -> object:
    """旧配置里已删除或已固定的键照常读取、静默丢弃，不报错（D107）。"""
    if isinstance(value, Mapping):
        return {key: item for key, item in value.items() if key not in legacy_keys}
    return value


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
    # 每分钟最多放行多少次调用；不写表示不限（默认，D135）。
    max_calls_per_minute: int | None = Field(default=None, ge=1)
    # 模型测速的独立每分钟上限，不占用上面的名额；不写表示不限（默认，D135）。
    model_test_calls_per_minute: int | None = Field(default=None, ge=1)
    max_consecutive_failures: int = Field(default=5, ge=1)
    failure_pause_seconds: int = Field(default=300, ge=1)
    # 网关同时放行的器官调用数（D117，修订 D48 的串行做法）。
    max_concurrent_organ_calls: int = Field(default=5, ge=1, le=8)


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
    # 正文就绪后最多再等表情多久，超时就只发文字（D117）。
    emoji_selection_grace_seconds: float = Field(default=0.8, ge=0, le=5)
    reply_organ: ClassVar[str] = "replyer"
    # 兼容原配置。各档位的回复流程都先请求规划器。
    planning_enabled: bool = True
    # D94：只关闭自主翻阅及规划器的特别关心调整，保留重要来信唤醒。
    inbox_attention_enabled: bool = True
    inbox_read_limit: int = Field(default=35, ge=1, le=200)
    inbox_debounce_enabled: bool = True
    inbox_debounce_seconds: float = Field(default=4.0, gt=0, le=60, allow_inf_nan=False)
    # 防抖最多等这么久（从这一批最早的消息算），避免人多的群里一直等不到安静窗口（D138）。
    # 必须不小于 inbox_debounce_seconds。
    inbox_debounce_max_wait_seconds: float = Field(default=20.0, ge=1, le=120, allow_inf_nan=False)
    inbox_debounce_max_restarts: int = Field(default=4, ge=0, le=20)
    plan_organ: ClassVar[str] = "planner"
    qq_emoji_collection_enabled: bool = False
    qq_emoji_selection_enabled: bool = True
    qq_emoji_maintenance_enabled: bool = True
    qq_emoji_registration_limit: int = Field(default=DEFAULT_EMOJI_REGISTRATION_LIMIT, ge=1)
    qq_emoji_replacement_suggestion_enabled: bool = False
    qq_emoji_visual_enabled: bool = True
    qq_emoji_semantic_refresh_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    qq_emoji_semantic_threshold: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)
    # 节省档、低电量（含透支保护）时带上最近多少条对话；都不会超过 recent_message_limit。
    saving_message_limit: int = Field(default=10, ge=1)
    low_message_limit: int = Field(default=5, ge=1)
    # 说话方式（路线图 1.7，D60）：把一段回复拆成几条短消息发出。关掉后整段一条发出。
    split_reply_enabled: bool = True
    # 一段回复最多拆成几条。
    max_reply_messages: int = Field(default=4, ge=1)
    # 一条至少多少个字；更短的句子并进相邻的一句。
    min_message_chars: int = Field(default=4, ge=1)
    typing_delay_enabled: bool = True
    typing_han_seconds: float = Field(default=0.9, ge=0, allow_inf_nan=False)
    typing_other_seconds: float = Field(default=0.38, ge=0, allow_inf_nan=False)
    typing_jitter_min_seconds: float = Field(default=0.3, ge=0, allow_inf_nan=False)
    typing_jitter_max_seconds: float = Field(default=1.7, ge=0, allow_inf_nan=False)
    # 用户在 identity.toml 里改了设定后，这次运行的系统上下文里告诉它哪几层被改了（D62）。
    # 只说哪几层，不给旧内容；时间线照样记，关掉只影响它知不知道。
    identity_changes_in_prompt: bool = True
    # 扮演模式（路线图 1.12，D65）：实验对照组，默认关闭。开启时 open_core 不创建身体和样本存储，
    # 所有身体信号机制都不起作用，系统上下文里只写一段固定的「你有精力和心情」让模型自己演。
    # 月度上限、失控拦截、急停、权限检查这些安全检查照常生效。
    roleplay_mode: bool = False
    working_memory_enabled: bool = True
    working_memory_capacity: int = Field(default=80, ge=2, le=300)
    working_memory_retain: int = Field(default=25, ge=1)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_organ_names(cls, value: object) -> object:
        """旧配置可照常读取，但规划和回复固定走内置器官的模型绑定。

        表情语义开关已删，向量模型配好后自动启用，旧键同样丢弃（D107）。
        """
        return drop_legacy_keys(value, ("plan_organ", "reply_organ", "qq_emoji_semantic_enabled"))

    @model_validator(mode="before")
    @classmethod
    def default_debounce_max_wait(cls, value: object) -> object:
        """不写上限时默认 20 秒；防抖窗口更大时自动取窗口本身，旧配置照常读取（D138）。"""
        if isinstance(value, dict) and "inbox_debounce_max_wait_seconds" not in value:
            debounce = value.get("inbox_debounce_seconds")
            if (
                isinstance(debounce, (int, float))
                and not isinstance(debounce, bool)
                and debounce > 20.0
            ):
                return {**value, "inbox_debounce_max_wait_seconds": float(debounce)}
        return value

    @model_validator(mode="after")
    def validate_typing_jitter(self) -> Self:
        """打字随机延迟的下限不能超过上限。"""
        if self.typing_jitter_min_seconds > self.typing_jitter_max_seconds:
            raise ValueError("typing_jitter_min_seconds 不能超过 typing_jitter_max_seconds")
        return self

    @model_validator(mode="after")
    def validate_working_memory_window(self) -> Self:
        """保留的近期焦点必须比工作记忆容量小。"""
        if self.working_memory_retain >= self.working_memory_capacity:
            raise ValueError("working_memory_retain 必须小于 working_memory_capacity")
        return self

    @model_validator(mode="after")
    def validate_debounce_wait(self) -> Self:
        """防抖最多等待不能比防抖窗口还短。"""
        if self.inbox_debounce_max_wait_seconds < self.inbox_debounce_seconds:
            raise ValueError("inbox_debounce_max_wait_seconds 不能小于 inbox_debounce_seconds")
        return self


class IndexingConfig(BaseModel):
    """配置好嵌入模型后自动持续建索引（aureus.toml 的 [indexing]，设计见 D125）。"""

    # 严格类型，理由同 SafetyConfig：开关写成数字、条数写成字符串要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 总开关（D5）。关掉后回到回复后按需补建、人工维护和收到图片时索引。
    auto_build_enabled: bool = True
    # 后台构建器每批最多处理多少条表达、表情描述或图片；范围 1–200。
    batch_size: int = Field(default=32, ge=1, le=200)
    # 两批之间等待多少秒；0 表示不额外等待，让出一拍就继续。
    interval_seconds: float = Field(default=5.0, ge=0, le=3600, allow_inf_nan=False)


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
    # 压力偏高时回复做法比精力档位低一档（D70）；回落到偏高线一半以下才恢复。
    # 关掉后压力只自检、不降档。
    stress_lowers_tier: bool = True

    # 社交需求（D76）：距上次和用户有效对话越久，越想主动聊聊。
    social_need_enabled: bool = True
    # 上涨曲线的时间常数（小时），一个时间常数后约为 63%。
    social_need_time_constant_hours: float = Field(default=12.0, gt=0)
    # 达到这个比例，才可能成为主动联系的原因。
    social_need_high_at: float = Field(default=0.7, gt=0, le=1)

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


class InitiativeConfig(BaseModel):
    """主动联系用户的配置（aureus.toml 的 [initiative]，设计见 D76）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 主动联系默认关闭，只有用户明确打开后才考虑。
    enabled: bool = False
    # 每个预算日最多主动发出几次；谨慎时减半，至少一次。
    max_per_day: int = Field(default=2, ge=1)
    # 每连续一次没回应，考虑间隔翻倍，最多到上限（小时）。
    min_interval_hours: float = Field(default=3.0, gt=0)
    max_interval_hours: float = Field(default=48.0, gt=0)
    # 发出后等用户回应多久（小时）。
    answer_window_hours: float = Field(default=6.0, gt=0)
    # 连续几次没回应后停止，等用户先开口。
    give_up_after: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.min_interval_hours > self.max_interval_hours:
            raise ValueError("min_interval_hours 不能大于 max_interval_hours")
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
    port: int = Field(default=DEFAULT_WEB_PORT, ge=1024, le=65535)
    # 登录状态的有效天数，从最后一次活动算起，每次请求续期（D63 第 7 条）。
    session_days: int = Field(default=7, ge=1)
    # 登录失败限流（按 IP）：窗口内失败这么多次，就在锁定期内拒绝这个 IP 的登录（D63 第 7 条）。
    login_max_failures: int = Field(default=7, ge=1)
    login_window_seconds: int = Field(default=300, ge=1)
    login_lockout_seconds: int = Field(default=30, ge=1)
    qq_avatars_enabled: bool = True


class QQConfig(BaseModel):
    """QQ 正向 WebSocket 的部署参数（路线图 3.6）"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    enabled: bool = False
    endpoint: str = ""
    account_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{0,19})?$")
    # 不写 = 所有群和私聊；填写后只允许该群和本人普通私聊（D136）。
    allowed_group_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    token_key: str = Field(default="", pattern=r"^(?:AUREUS_KEY_[A-Z0-9_]+)?$")
    action_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    reconnect_seconds: float = Field(default=5.0, gt=0, le=120)

    @model_validator(mode="after")
    def check_endpoint(self) -> "QQConfig":
        """启用时要求明确的账号与端点。凭据只由密钥接口读取"""
        if not self.enabled:
            return self
        try:
            url = urlsplit(self.endpoint)
            port = url.port
        except ValueError:
            raise ValueError("QQ 通道的 WebSocket 端点无效") from None
        if (
            not self.account_id
            or not url.hostname
            or url.scheme not in ("ws", "wss")
            or url.username is not None
            or port == 0
            or "?" in self.endpoint
            or "#" in self.endpoint
            or any(character.isspace() for character in self.endpoint)
        ):
            raise ValueError("QQ 通道需要账号和不含凭据、查询参数或片段的 WebSocket 端点")
        if url.scheme == "ws" and url.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("QQ 的 ws 端点仅限回环地址，远程端点使用 wss")
        return self


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
    state_context_enabled: bool = True
    # 固定使用 models.toml 里的 judge 器官（D107）；没配时跳过判断，不报错（D68 第 7 条）。
    organ: ClassVar[str] = "judge"
    # 判断最多等多久。判断只是回复前的参考，等太久不如直接回复；默认 120 秒，
    # 和服务商超时一致（D135，取代 D68 的 10 秒）。
    timeout_seconds: float = Field(default=120.0, gt=0)
    # 除了这次要回复的消息，再带之前几条对话；0 表示只看这一条。
    context_messages: int = Field(default=5, ge=0)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_organ(cls, value: object) -> object:
        """旧配置里的 organ 照常读取、忽略（D107）。"""
        return drop_legacy_keys(value, ("organ",))


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


class CortisolConfig(BaseModel):
    """皮质醇启发的主动联系调节（D89、D91 与 3.8）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = False
    # 上升段参考应激实验的采样时程，半衰期参考内源性分泌估计。行为校准见任务 3.8。
    rise_seconds: float = Field(default=600.0, ge=1, le=86400)
    half_life_seconds: float = Field(default=3960.0, ge=1, le=86400)
    # 无量纲软件参数，行为门槛由 3.7 离线回放校准。
    baseline_span: float = Field(default=0.4, ge=0, le=1)
    failure_peak: float = Field(default=0.55, ge=0, le=1)
    defer_at: float = Field(default=0.75, gt=0, le=1)


class PeopleConfig(BaseModel):
    """人际的配置（aureus.toml 的 [people]，好感的设计见 docs/10-decisions.md 的 D71）。

    各项基础量和上限都是随手挑的初值，没有实测依据，跑起来之后按表现调整。
    """

    # 严格类型，理由同 SafetyConfig：好感写成字符串、开关写成数字要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 3.3 的迁移人物资料只影响正常回复，关掉后仍可查询来源。
    history_in_reply_enabled: bool = True
    # 回复上下文里最多带几条这个人的迁移资料；与记忆检索条数分开配置（D118）。
    history_in_reply_limit: int = Field(default=5, ge=1, le=20)
    profile_alias_maintenance_in_reply_enabled: bool = True
    profile_content_maintenance_in_reply_enabled: bool = True
    # 好感的总开关（D5）；关掉后不记变化、不进上下文。
    affinity_enabled: bool = True
    # 好感满分 100。初值只在第一次用到时采用，之后改配置不会回头改已有的好感（D71 第 3 条）。
    # 取不到 0 和 100：公式保证两端永远到不了，初值放在端点上就再也动不了了。
    user_initial_affinity: float = Field(default=80.0, gt=0, lt=100)
    stranger_affinity: float = Field(default=30.0, gt=0, lt=100)
    # 判断器官报的事：基础变化量 = 基础量 × 强度 × 把握，再按好感的高低打折（D66 第 3 条）。
    praise_amount: float = Field(default=3.0, ge=0)
    offense_amount: float = Field(default=4.0, ge=0)
    rejection_amount: float = Field(default=2.0, ge=0)
    # 每个「一天」第一次有判断过的对话时，按互动量涨这么多（同样按好感打折）。
    interaction_amount: float = Field(default=0.5, ge=0)
    # 每个人每天涨、跌各自的上限，刷屏刷不动（D71 第 2 条）。
    daily_rise_cap: float = Field(default=5.0, ge=0)
    daily_fall_cap: float = Field(default=8.0, ge=0)


class EmpathyConfig(BaseModel):
    """同理心的配置（aureus.toml 的 [empathy]，设计见 docs/10-decisions.md 的 D72）。

    单次上限、刷屏递减、窗口和最低把握复用 [hormones] 的，不另设（D72 第 3 条）。
    下面的数值是随手挑的初值，没有实测依据。
    """

    # 严格类型，理由同 SafetyConfig：开关写成数字要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 共情的开关（D5）。关掉后判断器官照常调用，只是不产生共情触发（D72 第 1 条）。
    enabled: bool = True
    # 基础量：共情上涨量 = 基础量 × 强度 × 把握 × 好感系数。比直接被冒犯的基础量小。
    base_amount: float = Field(default=0.3, gt=0, le=1)
    # 好感系数 = 下限 + (1 − 下限) × 好感 ÷ 100；下限大于 0，对讨厌的人共情变弱但不会反过来。
    affinity_floor: float = Field(default=0.2, ge=0, le=1)


class MoodConfig(BaseModel):
    """心情的配置（aureus.toml 的 [mood]，设计见 docs/10-decisions.md 的 D73）。

    下面的数值是随手挑的初值，没有实测依据，跑起来之后按表现调整。
    """

    # 严格类型，理由同 SafetyConfig：开关写成数字、数值写成字符串要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 心情的开关（D5）。关掉后不算心情、不进上下文、不谨慎。
    enabled: bool = True
    # 每件事对心情的影响按这个半衰期回落（秒），默认 6 小时：心情是几小时到几天的基调。
    half_life_seconds: float = Field(default=21600.0, gt=0)
    # 激素触发换成心情：贡献 = 权重 × 上涨量；愉悦记正，怒气和低落记负，惊讶不计。
    hormone_weight: float = Field(default=0.5, ge=0)
    # 反馈结果：成功加、失败减；失败分量更重。
    success_amount: float = Field(default=0.05, ge=0)
    failure_amount: float = Field(default=0.1, ge=0)
    # 每件事单次贡献的上限（绝对值）。
    max_single_change: float = Field(default=0.2, gt=0, le=1)
    # 此刻的压力往下拉多少：−权重 × 压力，不累积。
    stress_weight: float = Field(default=0.3, ge=0, le=1)
    # 低于 cautious_below 进入谨慎，回到 recover_above 以上才恢复（D73 第 5 条）。
    cautious_below: float = Field(default=-0.3, ge=-1, le=1)
    recover_above: float = Field(default=-0.15, ge=-1, le=1)

    @model_validator(mode="after")
    def _check_caution_levels(self) -> Self:
        if not self.cautious_below < self.recover_above:
            raise ValueError("要满足 cautious_below < recover_above")
        return self


class AgendaConfig(BaseModel):
    """日程的配置（aureus.toml 的 [agenda]，设计见 docs/10-decisions.md 的 D74）。

    这里管记下承诺、到期提醒和兑现记账；自己去推进日程、主动联系对方是 2.8 的 [initiative]。
    """

    # 严格类型，理由同 SafetyConfig：开关写成数字、条数写成字符串要报错，不悄悄转换。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 日程的开关（D5）。关掉后不记承诺、不提醒、不记兑现。
    enabled: bool = True
    # 每个人名下未完成的条目最多几条，超过的新承诺不记（防止被刷满）。
    max_open_per_person: int = Field(default=20, ge=1)
    # 到期时间最远能定在几天后；更远的只丢掉时间，条目照记。
    max_due_days: int = Field(default=90, ge=1)
    # 到期后再过几天还没交代，就算过期（承诺失败）。
    expire_grace_days: float = Field(default=3.0, ge=0)
    # 回复时上下文里最多列几条这个人名下未完成的条目。
    context_items: int = Field(default=5, ge=1)


class InterestsConfig(BaseModel):
    """兴趣的开关、容量和自行改动限额（D79）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = False
    max_active: int = Field(default=10, ge=1, le=50)
    max_changes_per_day: int = Field(default=3, ge=0)


class ExplorationConfig(BaseModel):
    """只读小探索的频率、网页限制和总结器官（D79）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = False
    # 固定使用 summarizer 器官（D107）。
    organ: ClassVar[str] = "summarizer"
    search_provider: Literal["none", "searxng"] = "none"
    search_endpoint: str = ""
    max_per_day: int = Field(default=3, ge=1)
    min_interval_hours: float = Field(default=4.0, gt=0)
    max_results: int = Field(default=5, ge=1, le=10)
    fetch_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_page_bytes: int = Field(default=1048576, ge=1024, le=10485760)
    max_page_chars: int = Field(default=6000, ge=200, le=20000)
    share_within_days: float = Field(default=3.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_organ(cls, value: object) -> object:
        """旧配置里的 organ 照常读取、忽略（D107）。"""
        return drop_legacy_keys(value, ("organ",))

    @model_validator(mode="after")
    def _check_search_endpoint(self) -> Self:
        if not self.search_endpoint and self.search_provider == "none":
            return self
        try:
            endpoint = urlsplit(self.search_endpoint)
            valid = (
                endpoint.scheme in ("http", "https")
                and bool(endpoint.hostname)
                and endpoint.username is None
                and not endpoint.query
                and not endpoint.fragment
                and not any(character.isspace() for character in self.search_endpoint)
                and endpoint.port != 0
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "search_endpoint 必须是完整的 HTTP(S) 搜索地址，且不能含凭据、查询参数或片段"
            )
        return self


class MemoryConfig(BaseModel):
    """增量记忆索引和按关键词召回的开关与上限（D80）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = True
    recall_enabled: bool = True
    heuristic_recall_enabled: bool = False
    graph_recall_enabled: bool = True
    recall_feedback_enabled: bool = True
    removal_operations_enabled: bool = True
    # 普通检索与融合后的最终条数；自然召回和查询工具各有自己的上限（D118）。
    max_results: int = Field(default=6, ge=1, le=20)
    # 每轮规划前按回忆提示词自然召回的条数（D118）。
    heuristic_max_results: int = Field(default=3, ge=1, le=20)
    # 规划器的 query_memory 不传 limit 时的条数（D118）。
    query_default_limit: int = Field(default=8, ge=1, le=20)
    index_batch: int = Field(default=200, ge=1, le=5000)
    consolidation_enabled: bool = False
    consolidation_batch: int = Field(default=64, ge=1, le=500)
    consolidation_half_life_days: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    consolidation_extract_enabled: bool = False


class LanguageConfig(BaseModel):
    """习得语言的召回、增量学习与表达向量候选上限（D89、D119）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    recall_enabled: bool = True
    # 器官绑定固定（D107）；向量索引随嵌入模型自动启用，不再有开关。
    organ: ClassVar[str] = "summarizer"
    expression_quality_enabled: bool = True
    # 质检固定走 judge：原默认为空时，开启质检会拒收全部新表达。
    expression_quality_organ: ClassVar[str] = "judge"
    # 表达向量召回最多取多少条候选，范围 1–50（D119）。
    expression_vector_candidates: int = Field(default=50, ge=1, le=50)
    # 表达选择固定走 fast_model（D119）；没有配置时退回向量前 5，不整池注入。
    expression_selector_organ: ClassVar[str] = "fast_model"

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_keys(cls, value: object) -> object:
        """旧配置里已删除或已固定的键照常读取、忽略（D107、D109、D120）。"""
        return drop_legacy_keys(
            value,
            (
                "organ",
                "expression_quality_organ",
                "expression_selector_organ",
                "use_vector_expression",
                "learning_enabled",
                "reference_chars",
            ),
        )


class DiaryConfig(BaseModel):
    """每日日记的器官、当地写作时刻和长度限制（D80）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = True
    # 固定使用 summarizer 器官（D107）。
    organ: ClassVar[str] = "summarizer"
    write_after_hour: int = Field(default=22, ge=0, le=23)
    max_input_chars: int = Field(default=6000, ge=1000, le=50000)
    max_chars: int = Field(default=1500, ge=100, le=10000)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_organ(cls, value: object) -> object:
        """旧配置里的 organ 照常读取、忽略（D107）。"""
        return drop_legacy_keys(value, ("organ",))


class PersonaGrowthConfig(BaseModel):
    """人设成长的慢速调整规则；会改自己的行为默认关闭（D81）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = False
    # 固定使用 summarizer 器官（D107）。
    organ: ClassVar[str] = "summarizer"
    max_input_chars: int = Field(default=6000, ge=1000, le=50000)
    layer_cooldown_days: int = Field(default=7, ge=1, le=365)
    min_similarity: float = Field(default=0.7, ge=0.3, le=0.99)
    max_chars_from_empty: int = Field(default=200, ge=20, le=1000)
    max_layer_chars: int = Field(default=1000, ge=100, le=5000)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_organ(cls, value: object) -> object:
        """旧配置里的 organ 照常读取、忽略（D107）。"""
        return drop_legacy_keys(value, ("organ",))


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
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    body: BodyConfig = Field(default_factory=BodyConfig)
    time: TimeConfig = Field(default_factory=TimeConfig)
    snapshots: SnapshotsConfig = Field(default_factory=SnapshotsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    hormones: HormonesConfig = Field(default_factory=HormonesConfig)
    cortisol: CortisolConfig = Field(default_factory=CortisolConfig)
    people: PeopleConfig = Field(default_factory=PeopleConfig)
    empathy: EmpathyConfig = Field(default_factory=EmpathyConfig)
    mood: MoodConfig = Field(default_factory=MoodConfig)
    agenda: AgendaConfig = Field(default_factory=AgendaConfig)
    initiative: InitiativeConfig = Field(default_factory=InitiativeConfig)
    interests: InterestsConfig = Field(default_factory=InterestsConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    language: LanguageConfig = Field(default_factory=LanguageConfig)
    diary: DiaryConfig = Field(default_factory=DiaryConfig)
    persona_growth: PersonaGrowthConfig = Field(default_factory=PersonaGrowthConfig)

    @model_validator(mode="after")
    def _check_exploration(self) -> Self:
        if self.exploration.enabled and not self.interests.enabled:
            raise ValueError("exploration 开启时 interests 也必须开启")
        return self

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
    lines = [f"{source} 有问题："]
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        field_name = ".".join(str(part) for part in item["loc"]) or "（整个文件）"
        reason = _REASON_BY_ERROR_TYPE.get(item["type"], f"格式不对（{item['msg']}）")
        lines.append(f"  - 字段 {field_name}：{reason}")
    return "\n".join(lines)


def validate_runtime_config(raw: Mapping[str, Any], source: str) -> RuntimeConfig:
    """校验运行配置，来源说明由调用方提供；失败抛 ConfigError，不回显填入的值。"""
    try:
        config = RuntimeConfig.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(_describe_errors(source, error)) from None
    if config.config_version > CONFIG_FORMAT_VERSION:
        raise ConfigError(
            f"{source} 的 config_version 是 {config.config_version}，"
            f"比当前软件认识的版本 {CONFIG_FORMAT_VERSION} 更新。请先升级软件。"
        )
    return config


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
    return validate_runtime_config(raw, f"配置文件 {path}")


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
