"""配置读取的测试：运行配置和报错信息。"""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from aureus.common import (
    AureusPaths,
    BodyConfig,
    ConfigError,
    HeartbeatConfig,
    SafetyConfig,
    TimeConfig,
    WebConfig,
    load_runtime_config,
    load_settings,
)
from aureus.common.config import CONFIG_FORMAT_VERSION


def write_runtime_config(paths: AureusPaths, text: str) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_file.write_text(text, encoding="utf-8")


def error_message_of(paths: AureusPaths) -> str:
    with pytest.raises(ConfigError) as info:
        load_runtime_config(paths)
    return str(info.value)


# ---------- 运行配置 ----------


def test_missing_config_file_gives_defaults(tmp_path: Path) -> None:
    config = load_runtime_config(AureusPaths(tmp_path))
    assert config.log_level == "INFO"
    assert config.config_version == CONFIG_FORMAT_VERSION


def test_values_from_file_are_used_and_level_is_case_insensitive(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, 'log_level = "debug"\n')
    assert load_runtime_config(paths).log_level == "DEBUG"


def test_unknown_field_is_reported_with_file_and_field(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, 'log_levle = "INFO"\n')
    message = error_message_of(paths)
    assert str(paths.runtime_config_file) in message
    assert "log_levle" in message
    assert "不认识" in message


def test_invalid_value_names_the_field_but_does_not_echo_the_value(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, 'log_level = "SUPER-LOUD-VALUE"\n')
    message = error_message_of(paths)
    assert "log_level" in message
    assert "SUPER-LOUD-VALUE" not in message


def test_wrong_type_is_reported(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, 'config_version = "one"\n')
    assert "config_version" in error_message_of(paths)


def test_broken_toml_is_reported_with_file(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "log_level = = \n")
    message = error_message_of(paths)
    assert "TOML" in message
    assert str(paths.runtime_config_file) in message


def test_config_from_newer_software_is_rejected(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"config_version = {CONFIG_FORMAT_VERSION + 1}\n")
    assert "更新" in error_message_of(paths)


def test_unreadable_config_path_is_reported(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.runtime_config_file.mkdir()  # 同名的是文件夹，读不了
    assert str(paths.runtime_config_file) in error_message_of(paths)


# ---------- [safety] 安全网关配置 ----------


def test_safety_defaults_when_section_is_missing(tmp_path: Path) -> None:
    safety = load_runtime_config(AureusPaths(tmp_path)).safety
    assert safety == SafetyConfig()
    assert safety.monthly_token_limit is None  # 默认不限额
    assert safety.runaway_guard_enabled is True  # 默认开启失控拦截
    assert safety.max_calls_per_minute == 20
    assert safety.max_consecutive_failures == 5
    assert safety.failure_pause_seconds == 300


def test_safety_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[safety]\n"
        "monthly_token_limit = 1000\n"
        "runaway_guard_enabled = false\n"
        "max_calls_per_minute = 3\n"
        "max_consecutive_failures = 2\n"
        "failure_pause_seconds = 10\n",
    )
    safety = load_runtime_config(paths).safety
    assert safety.monthly_token_limit == 1000
    assert safety.runaway_guard_enabled is False
    assert safety.max_calls_per_minute == 3
    assert safety.max_consecutive_failures == 2
    assert safety.failure_pause_seconds == 10


def test_safety_example_config_is_valid_when_uncommented(tmp_path: Path) -> None:
    # 模板里注释掉的示例值，取消注释后必须真的能被读进来，免得模板和代码悄悄对不上。
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    uncommented = "\n".join(
        line.removeprefix("# ")
        if line.startswith(("# monthly_", "# runaway_", "# max_", "# failure_"))
        else line
        for line in example.read_text(encoding="utf-8").splitlines()
    )
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, uncommented)
    safety = load_runtime_config(paths).safety
    assert safety.monthly_token_limit is not None
    assert safety.runaway_guard_enabled is True


@pytest.mark.parametrize("value", ["0", "-5"])
def test_safety_monthly_limit_must_be_positive(tmp_path: Path, value: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[safety]\nmonthly_token_limit = {value}\n")
    assert "monthly_token_limit" in error_message_of(paths)


@pytest.mark.parametrize(
    "field", ["max_calls_per_minute", "max_consecutive_failures", "failure_pause_seconds"]
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_safety_limits_must_be_at_least_one(tmp_path: Path, field: str, value: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[safety]\n{field} = {value}\n")
    assert field in error_message_of(paths)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("monthly_token_limit", "true"),  # true 不能悄悄变成 1
        ("monthly_token_limit", '"1000"'),  # 带引号的数字不是数字
        ("max_calls_per_minute", "true"),
        ("runaway_guard_enabled", "1"),  # 数字 1 不是 true
        ("runaway_guard_enabled", '"false"'),  # 字符串 "false" 不能当成 false
    ],
)
def test_safety_is_strict_about_types(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[safety]\n{field} = {text}\n")
    assert field in error_message_of(paths)


def test_safety_unknown_field_is_reported(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[safety]\nmonthly_token_limt = 100\n")
    message = error_message_of(paths)
    assert "monthly_token_limt" in message
    assert "不认识" in message


def test_safety_error_does_not_echo_unsafe_value(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, '[safety]\nmonthly_token_limit = "SUPER-LOUD-VALUE"\n')
    assert "SUPER-LOUD-VALUE" not in error_message_of(paths)


# ---------- [heartbeat] 心跳配置（D41） ----------

HEARTBEAT_FIELDS = tuple(HeartbeatConfig.model_fields)


def test_heartbeat_defaults_when_section_is_missing(tmp_path: Path) -> None:
    heartbeat = load_runtime_config(AureusPaths(tmp_path)).heartbeat
    assert heartbeat == HeartbeatConfig()
    assert heartbeat.tick_interval_seconds == 60
    assert heartbeat.advance_biomodel is True  # 0.9a 修好模型后默认开（D42 第 8 条）
    assert heartbeat.broadcast_cognitive_state is True
    assert heartbeat.reply_organ == "replyer"


def test_heartbeat_example_values_equal_the_defaults(tmp_path: Path) -> None:
    # 模板说“下面的值都是默认值”：取消注释后读进来，必须和代码里的默认值一模一样。
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in HEARTBEAT_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    uncommented = [line.removeprefix("# ") if line.startswith(prefixes) else line for line in lines]
    assert sum(line.startswith(prefixes) for line in lines) == len(HEARTBEAT_FIELDS)
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(uncommented))
    assert load_runtime_config(paths).heartbeat == HeartbeatConfig()


def test_heartbeat_integer_seconds_are_accepted(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[heartbeat]\ntick_interval_seconds = 5\n")
    assert load_runtime_config(paths).heartbeat.tick_interval_seconds == 5.0


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("tick_interval_seconds", "0.5"),  # 小于 1 秒会让心跳空转
        ("tick_interval_seconds", "0"),
        ("shutdown_grace_seconds", "-1"),
        ("recent_message_limit", "0"),
        ("reply_organ", '""'),
        ("advance_biomodel", "1"),  # 数字 1 不是 true
        ("broadcast_cognitive_state", '"false"'),
        ("tick_interval_seconds", '"60"'),
        ("tick_interval_seconds", "true"),
    ],
)
def test_heartbeat_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[heartbeat]\n{field} = {text}\n")
    assert field in error_message_of(paths)


# ---------- [body] 身体信号的可调参数（D56） ----------

BODY_FIELDS = tuple(BodyConfig.model_fields)


def test_body_defaults_when_section_is_missing(tmp_path: Path) -> None:
    body = load_runtime_config(AureusPaths(tmp_path)).body
    assert body == BodyConfig()
    assert body.energy_enabled is True
    assert (body.user_reserve_fraction, body.saving_below, body.low_below) == (0.15, 0.5, 0.2)
    assert body.overdraft_below == 0.05


def test_body_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in BODY_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    uncommented = [line.removeprefix("# ") if line.startswith(prefixes) else line for line in lines]
    assert sum(line.startswith(prefixes) for line in lines) == len(BODY_FIELDS)
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(uncommented))
    assert load_runtime_config(paths).body == BodyConfig()


def test_body_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[body]\nenergy_enabled = false\nsaving_below = 0.6\nlow_below = 0.3\n"
        "overdraft_below = 0\nuser_reserve_fraction = 0.1\n",
    )
    body = load_runtime_config(paths).body
    assert body.energy_enabled is False
    assert (body.saving_below, body.low_below, body.overdraft_below) == (0.6, 0.3, 0.0)
    assert body.user_reserve_fraction == 0.1


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("user_reserve_fraction", "-0.1"),
        ("user_reserve_fraction", "1"),  # 全部留给用户，可选的调用永远不放行，不如关掉精力信号
        ("saving_below", "0"),
        ("saving_below", "1.5"),
        ("low_below", "nan"),
        ("overdraft_below", "-0.01"),
        ("energy_enabled", "1"),  # 数字 1 不是 true
        ("low_below", '"0.2"'),
    ],
)
def test_body_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[body]\n{field} = {text}\n")
    assert field in error_message_of(paths)


@pytest.mark.parametrize(
    "text",
    [
        "saving_below = 0.2\nlow_below = 0.2\n",  # 相等：中间那一档没有宽度
        "low_below = 0.6\n",  # 比默认的 saving_below 还大
        "overdraft_below = 0.3\n",  # 比默认的 low_below 还大
    ],
)
def test_body_tiers_must_be_in_order(tmp_path: Path, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[body]\n{text}")
    assert "档位分界" in error_message_of(paths)


def test_old_biomodel_section_is_reported_as_unknown(tmp_path: Path) -> None:
    # 精力、社交需求、新奇感已经不在生物模型里（D53、D56），旧配置要明确报错而不是悄悄忽略。
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[biomodel]\nenergy_recovery_rate = 0.1\n")
    assert "biomodel" in error_message_of(paths)


# ---------- [web] 网页通道配置（D63） ----------

WEB_FIELDS = tuple(WebConfig.model_fields)


def test_web_defaults_when_section_is_missing(tmp_path: Path) -> None:
    web = load_runtime_config(AureusPaths(tmp_path)).web
    assert web == WebConfig()
    assert web.enabled is True
    assert web.allow_remote is False  # 默认只监听本机（D53 第 5 条）
    assert web.session_days == 7  # D63 第 7 条


def test_web_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in WEB_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    uncommented = [line.removeprefix("# ") if line.startswith(prefixes) else line for line in lines]
    assert sum(line.startswith(prefixes) for line in lines) == len(WEB_FIELDS)
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(uncommented))
    assert load_runtime_config(paths).web == WebConfig()


def test_web_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[web]\nenabled = false\nallow_remote = true\nport = 9000\nsession_days = 3\n"
        "login_max_failures = 2\nlogin_window_seconds = 60\nlogin_lockout_seconds = 120\n",
    )
    web = load_runtime_config(paths).web
    assert (web.enabled, web.allow_remote, web.port, web.session_days) == (False, True, 9000, 3)
    assert (web.login_max_failures, web.login_window_seconds) == (2, 60)
    assert web.login_lockout_seconds == 120


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("port", "80"),  # 低端口在不少系统上要管理员权限
        ("port", "1023"),
        ("port", "65536"),
        ("port", '"8765"'),  # 带引号的数字不是数字
        ("port", "8765.0"),
        ("allow_remote", "1"),  # 数字 1 不是 true：对外开放不能靠误写打开
        ("allow_remote", '"true"'),
        ("enabled", '"false"'),
        ("session_days", "0"),
        ("login_max_failures", "0"),
        ("login_window_seconds", "-1"),
        ("login_lockout_seconds", "0"),
    ],
)
def test_web_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[web]\n{field} = {text}\n")
    assert f"web.{field}" in error_message_of(paths)


def test_web_unknown_field_is_reported(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[web]\nallow_remot = true\n")
    message = error_message_of(paths)
    assert "allow_remot" in message
    assert "不认识" in message


# ---------- 合在一起 ----------


# ---------- [time]：一天和一个月 ----------


def test_time_defaults_use_this_computers_timezone_and_four_oclock(tmp_path: Path) -> None:
    time = load_runtime_config(AureusPaths(tmp_path)).time
    assert time == TimeConfig()
    assert time.timezone is None
    assert time.day_start_hour == 4
    assert time.build_calendar().timezone is None


def test_time_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, '[time]\ntimezone = "Asia/Tokyo"\nday_start_hour = 5\n')
    calendar = load_runtime_config(paths).time.build_calendar()
    assert str(calendar.timezone) == "Asia/Tokyo"
    assert calendar.day_of(datetime(2026, 11, 18, 19, 30, tzinfo=UTC)) == date(2026, 11, 18)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("timezone", '"Mars/Base"'),
        ("timezone", '""'),
        ("timezone", "8"),
        ("day_start_hour", "24"),
        ("day_start_hour", "-1"),
        ("day_start_hour", '"4"'),
        ("day_start_hour", "4.5"),
    ],
)
def test_time_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[time]\n{field} = {text}\n")
    message = error_message_of(paths)
    assert f"time.{field}" in message
    assert "Mars" not in message


def test_load_settings_combines_paths_and_runtime(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, 'log_level = "WARNING"\n')
    settings = load_settings(paths)
    assert settings.paths == paths
    assert settings.runtime.log_level == "WARNING"
