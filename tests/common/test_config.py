"""配置读取的测试：运行配置和报错信息。"""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from aureus.common import (
    AureusPaths,
    BiomodelConfig,
    ConfigError,
    HeartbeatConfig,
    SafetyConfig,
    TimeConfig,
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


# ---------- [biomodel] 生物模型的可调参数（D43） ----------

BIOMODEL_FIELDS = tuple(BiomodelConfig.model_fields)


def test_biomodel_defaults_when_section_is_missing(tmp_path: Path) -> None:
    assert load_runtime_config(AureusPaths(tmp_path)).biomodel == BiomodelConfig()


def test_biomodel_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in BIOMODEL_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    uncommented = [line.removeprefix("# ") if line.startswith(prefixes) else line for line in lines]
    assert sum(line.startswith(prefixes) for line in lines) == len(BIOMODEL_FIELDS)
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(uncommented))
    assert load_runtime_config(paths).biomodel == BiomodelConfig()


def test_biomodel_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths, "[biomodel]\nsocial_timeout_per_hour = 0.1\nenergy_recovery_rate = 0\n"
    )
    biomodel = load_runtime_config(paths).biomodel
    assert biomodel.social_timeout_per_hour == 0.1
    assert biomodel.energy_recovery_rate == 0.0  # 写成整数也行；0 表示不恢复，合法


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("energy_recovery_rate", "-0.1"),  # 负数会让精力越来越低
        ("social_timeout_per_hour", "-1"),
        ("novelty_growth_per_hour", "nan"),  # 算进模型会让状态变成 nan
        ("novelty_growth_per_hour", "inf"),
        ("social_relief_per_conversation", "1.5"),  # 一次回落超过整个范围没有意义
        ("novelty_relief_per_conversation", "-0.01"),
        ("social_timeout_per_hour", '"0.03"'),
        ("energy_recovery_rate", "true"),
    ],
)
def test_biomodel_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[biomodel]\n{field} = {text}\n")
    assert field in error_message_of(paths)


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
