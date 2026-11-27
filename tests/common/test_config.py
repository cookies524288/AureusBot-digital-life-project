"""配置读取的测试：运行配置和报错信息。"""

import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from aureus.common import (
    AgendaConfig,
    AureusPaths,
    BodyConfig,
    ConfigError,
    DiaryConfig,
    EmpathyConfig,
    ExplorationConfig,
    FeedbackConfig,
    HeartbeatConfig,
    HormonesConfig,
    IndexingConfig,
    InitiativeConfig,
    InterestsConfig,
    JudgeConfig,
    LanguageConfig,
    MemoryConfig,
    MoodConfig,
    PeopleConfig,
    QQConfig,
    SafetyConfig,
    TimeConfig,
    WebConfig,
    load_runtime_config,
    load_settings,
    validate_runtime_config,
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


@pytest.mark.parametrize("text", ["[diary]\nwrite_after_hour = 99", "config_version = 99"])
def test_validate_runtime_config_keeps_the_file_error_except_for_source(
    tmp_path: Path, text: str
) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, text)
    file_message = error_message_of(paths)
    with pytest.raises(ConfigError) as error:
        validate_runtime_config(tomllib.loads(text), "提交的配置")
    assert str(error.value) == file_message.replace(
        f"配置文件 {paths.runtime_config_file}", "提交的配置"
    )
    assert str(paths.home) not in str(error.value)


@pytest.mark.parametrize(
    "section, defaults", [("memory", MemoryConfig()), ("diary", DiaryConfig())]
)
def test_memory_diary_defaults_and_example(
    tmp_path: Path, section: str, defaults: MemoryConfig | DiaryConfig
) -> None:
    paths = AureusPaths(tmp_path)
    assert getattr(load_runtime_config(paths), section) == defaults
    lines = Path("config/aureus.example.toml").read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, section)
    prefixes = tuple(f"# {name} =" for name in type(defaults).model_fields)
    assert sum(line.startswith(prefixes) for line in mine) == len(type(defaults).model_fields)
    start = lines.index(f"[{section}]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    write_runtime_config(paths, NEWLINE.join(lines))
    assert getattr(load_runtime_config(paths), section) == defaults


@pytest.mark.parametrize(
    "section, field, value",
    [
        ("memory", "enabled", "1"),
        ("memory", "recall_enabled", '"true"'),
        ("memory", "max_results", "0"),
        ("memory", "max_results", "21"),
        ("memory", "index_batch", "0"),
        ("memory", "index_batch", "5001"),
        ("memory", "index_batch", "true"),
        ("memory", "unknown", "1"),
        ("diary", "enabled", "1"),
        ("diary", "write_after_hour", "-1"),
        ("diary", "write_after_hour", "24"),
        ("diary", "max_input_chars", "999"),
        ("diary", "max_input_chars", "50001"),
        ("diary", "max_chars", "99"),
        ("diary", "max_chars", "10001"),
        ("diary", "max_chars", '"1500"'),
        ("diary", "unknown", "1"),
    ],
)
def test_memory_diary_reject_invalid_values(
    tmp_path: Path, section: str, field: str, value: str
) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[{section}]\n{field} = {value}\n")
    assert field in error_message_of(paths)


def test_memory_and_diary_switches_are_independent(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[memory]\nenabled = false\nrecall_enabled = true\n")
    config = load_runtime_config(paths)
    assert not config.memory.enabled and config.memory.recall_enabled and config.diary.enabled


def test_memory_config_defaults_match_mai_mai_bot() -> None:
    """D118：自然召回 3、普通检索 6、查询工具 8、人物历史 5。"""
    memory = MemoryConfig()
    assert memory.max_results == 6
    assert memory.heuristic_max_results == 3
    assert memory.query_default_limit == 8
    assert PeopleConfig().history_in_reply_limit == 5


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("memory", "max_results"),
        ("memory", "heuristic_max_results"),
        ("memory", "query_default_limit"),
        ("people", "history_in_reply_limit"),
    ],
)
@pytest.mark.parametrize("value", [0, 21])
def test_memory_limit_bounds_are_validated(
    tmp_path: Path, section: str, field: str, value: int
) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[{section}]\n{field} = {value}\n")
    assert field in error_message_of(paths)


def test_saved_max_results_still_wins(tmp_path: Path) -> None:
    """旧配置里显式的 max_results 继续生效，其余两项采用新默认值（D118）。"""
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[memory]\nmax_results = 9\n")
    memory = load_runtime_config(paths).memory
    assert memory.max_results == 9
    assert (memory.heuristic_max_results, memory.query_default_limit) == (3, 8)


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
    assert safety.max_calls_per_minute is None  # D135：默认不限流
    assert safety.model_test_calls_per_minute is None
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
        "model_test_calls_per_minute = 30\n"
        "max_consecutive_failures = 2\n"
        "failure_pause_seconds = 10\n",
    )
    safety = load_runtime_config(paths).safety
    assert safety.monthly_token_limit == 1000
    assert safety.runaway_guard_enabled is False
    assert safety.max_calls_per_minute == 3
    assert safety.model_test_calls_per_minute == 30
    assert safety.max_consecutive_failures == 2
    assert safety.failure_pause_seconds == 10


def test_safety_example_config_is_valid_when_uncommented(tmp_path: Path) -> None:
    # 模板里注释掉的示例值，取消注释后必须真的能被读进来，免得模板和代码悄悄对不上。
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    uncommented = "\n".join(
        line.removeprefix("# ")
        if line.startswith(("# monthly_", "# runaway_", "# max_", "# model_", "# failure_"))
        else line
        for line in example.read_text(encoding="utf-8").splitlines()
    )
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, uncommented)
    safety = load_runtime_config(paths).safety
    assert safety.monthly_token_limit is not None
    assert safety.runaway_guard_enabled is True
    assert safety.model_test_calls_per_minute is not None


@pytest.mark.parametrize("value", ["0", "-5"])
def test_safety_monthly_limit_must_be_positive(tmp_path: Path, value: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[safety]\nmonthly_token_limit = {value}\n")
    assert "monthly_token_limit" in error_message_of(paths)


@pytest.mark.parametrize(
    "field",
    [
        "max_calls_per_minute",
        "model_test_calls_per_minute",
        "max_consecutive_failures",
        "failure_pause_seconds",
        "max_concurrent_organ_calls",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_safety_limits_must_be_at_least_one(tmp_path: Path, field: str, value: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[safety]\n{field} = {value}\n")
    assert field in error_message_of(paths)


def test_organ_concurrency_cannot_exceed_the_gateway_limit(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[safety]\nmax_concurrent_organ_calls = 9\n")
    assert "max_concurrent_organ_calls" in error_message_of(paths)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("monthly_token_limit", "true"),  # true 不能悄悄变成 1
        ("monthly_token_limit", '"1000"'),  # 带引号的数字不是数字
        ("max_calls_per_minute", "true"),
        ("max_calls_per_minute", '"20"'),
        ("model_test_calls_per_minute", "true"),
        ("model_test_calls_per_minute", '"60"'),
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

HEARTBEAT_FIELDS = tuple(
    name
    for name in HeartbeatConfig.model_fields
    if name not in {"advance_biomodel", "planning_enabled"}
)


def test_heartbeat_defaults_when_section_is_missing(tmp_path: Path) -> None:
    heartbeat = load_runtime_config(AureusPaths(tmp_path)).heartbeat
    assert heartbeat == HeartbeatConfig()
    assert heartbeat.tick_interval_seconds == 60
    assert heartbeat.advance_biomodel is None  # 已废弃，没写就是 None（D69 第 7 条）
    assert heartbeat.broadcast_cognitive_state is True
    assert heartbeat.reply_organ == "replyer"
    assert heartbeat.plan_organ == "planner"
    assert heartbeat.message_debounce_mode == "steady"  # D142：本机 MaiBot 当前值
    assert heartbeat.planner_message_steering is True
    assert heartbeat.planner_steering_max_interrupts == 4
    assert heartbeat.inbox_read_limit == 15  # D145
    assert heartbeat.inbox_residence_seconds == 300
    assert heartbeat.mention_pull_enabled is True
    assert (heartbeat.working_memory_capacity, heartbeat.working_memory_retain) == (50, 20)  # D146


def test_working_memory_defaults_example_and_explicit_values(tmp_path: Path) -> None:
    """D146：每轮上下文容量默认 50、保留 20，模板与代码一致，显式值照常读取。"""
    defaults = HeartbeatConfig()
    assert (defaults.working_memory_capacity, defaults.working_memory_retain) == (50, 20)
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    lines = example.read_text(encoding="utf-8").splitlines()
    assert "# working_memory_capacity = 50" in lines
    assert "# working_memory_retain = 20" in lines
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths, "[heartbeat]\nworking_memory_capacity = 120\nworking_memory_retain = 40\n"
    )
    heartbeat = load_runtime_config(paths).heartbeat
    assert (heartbeat.working_memory_capacity, heartbeat.working_memory_retain) == (120, 40)
    # 去掉显式值后恢复默认 50/20。
    write_runtime_config(paths, "[heartbeat]\n")
    restored = load_runtime_config(paths).heartbeat
    assert (restored.working_memory_capacity, restored.working_memory_retain) == (50, 20)


def test_working_memory_retain_must_be_smaller_than_capacity(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths, "[heartbeat]\nworking_memory_capacity = 20\nworking_memory_retain = 20\n"
    )
    assert "working_memory_retain" in error_message_of(paths)


def test_legacy_organ_overrides_are_ignored_and_explicit_debounce_opt_out_is_kept(
    tmp_path: Path,
) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        '[heartbeat]\nreply_organ = "chatter"\nplan_organ = "legacy"\n'
        "inbox_debounce_enabled = false\n",
    )
    heartbeat = load_runtime_config(paths).heartbeat
    assert heartbeat.reply_organ == "replyer"
    assert heartbeat.plan_organ == "planner"
    assert heartbeat.message_debounce_mode == "off"
    assert heartbeat.planner_message_steering is False


def test_legacy_debounce_restart_limit_maps_to_the_steering_limit(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[heartbeat]\ninbox_debounce_enabled = true\ninbox_debounce_max_restarts = 2\n",
    )
    heartbeat = load_runtime_config(paths).heartbeat
    assert heartbeat.message_debounce_mode == "steady"
    assert heartbeat.planner_message_steering is True
    assert heartbeat.planner_steering_max_interrupts == 2


def test_legacy_debounce_seconds_are_dropped_without_error(tmp_path: Path) -> None:
    # 旧窗口和 20 秒上限由档位与整批 8 秒上限取代，旧配置照常启动（D142）。
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[heartbeat]\ninbox_debounce_seconds = 60.0\ninbox_debounce_max_wait_seconds = 30.0\n",
    )
    assert load_runtime_config(paths).heartbeat == HeartbeatConfig()


def test_explicit_debounce_settings_win_over_legacy_fields(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[heartbeat]\ninbox_debounce_enabled = false\ninbox_debounce_max_restarts = 2\n"
        'message_debounce_mode = "natural"\nplanner_message_steering = true\n'
        "planner_steering_max_interrupts = 1\n",
    )
    heartbeat = load_runtime_config(paths).heartbeat
    assert heartbeat.message_debounce_mode == "natural"
    assert heartbeat.planner_message_steering is True
    assert heartbeat.planner_steering_max_interrupts == 1


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


def test_explicit_inbox_read_limit_still_wins(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[heartbeat]\ninbox_read_limit = 72\n")
    assert load_runtime_config(paths).heartbeat.inbox_read_limit == 72


@pytest.mark.parametrize("value", [1, 200])
def test_inbox_read_limit_bounds_are_accepted(tmp_path: Path, value: int) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[heartbeat]\ninbox_read_limit = {value}\n")
    assert load_runtime_config(paths).heartbeat.inbox_read_limit == value


@pytest.mark.parametrize("seconds", [0, 30, 30.5, 3600])
def test_residence_window_bounds_and_mention_opt_out(tmp_path: Path, seconds: float) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths, f"[heartbeat]\ninbox_residence_seconds = {seconds}\nmention_pull_enabled = false\n"
    )
    heartbeat = load_runtime_config(paths).heartbeat
    assert heartbeat.inbox_residence_seconds == seconds
    assert heartbeat.mention_pull_enabled is False


def test_roleplay_mode_is_off_by_default(tmp_path: Path) -> None:
    assert load_runtime_config(AureusPaths(tmp_path)).heartbeat.roleplay_mode is False


def test_roleplay_mode_can_be_turned_on_from_the_file(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[heartbeat]\nroleplay_mode = true\n")
    assert load_runtime_config(paths).heartbeat.roleplay_mode is True


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
        ("advance_biomodel", "1"),  # 数字 1 不是 true
        ("broadcast_cognitive_state", '"false"'),
        ("roleplay_mode", "1"),  # 数字 1 不是 true
        ("roleplay_mode", '"true"'),
        ("tick_interval_seconds", '"60"'),
        ("tick_interval_seconds", "true"),
        ("emoji_selection_grace_seconds", "-0.1"),
        ("emoji_selection_grace_seconds", "5.1"),
        ("message_debounce_mode", '"sometimes"'),
        ("planner_message_steering", "1"),  # 数字 1 不是 true
        ("planner_steering_max_interrupts", "-1"),
        ("planner_steering_max_interrupts", "21"),
        ("planner_steering_max_interrupts", "true"),
        ("planner_steering_max_interrupts", '"4"'),
        ("inbox_read_limit", "0"),
        ("inbox_read_limit", "201"),
        ("inbox_read_limit", "35.5"),
        ("inbox_read_limit", '"15"'),
        ("inbox_residence_seconds", "-1"),
        ("inbox_residence_seconds", "29.9"),
        ("inbox_residence_seconds", "3600.1"),
        ("inbox_residence_seconds", "nan"),
        ("inbox_residence_seconds", "inf"),
        ("inbox_residence_seconds", "true"),
        ("inbox_residence_seconds", '"300"'),
        ("mention_pull_enabled", "1"),
        ("mention_pull_enabled", '"true"'),
    ],
)
def test_heartbeat_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[heartbeat]\n{field} = {text}\n")
    assert field in error_message_of(paths)


# ---------- [indexing] 自动建索引（D125） ----------

INDEXING_FIELDS = tuple(IndexingConfig.model_fields)


def test_indexing_defaults_when_section_is_missing(tmp_path: Path) -> None:
    indexing = load_runtime_config(AureusPaths(tmp_path)).indexing
    assert indexing == IndexingConfig()
    assert indexing.auto_build_enabled is True
    assert (indexing.batch_size, indexing.interval_seconds) == (32, 5.0)


def test_indexing_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in INDEXING_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "indexing")
    assert sum(line.startswith(prefixes) for line in mine) == len(INDEXING_FIELDS)
    uncommented = [line.removeprefix("# ") if line.startswith(prefixes) else line for line in lines]
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(uncommented))
    assert load_runtime_config(paths).indexing == IndexingConfig()


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("auto_build_enabled", "1"),
        ("auto_build_enabled", '"true"'),
        ("batch_size", "0"),
        ("batch_size", "201"),
        ("batch_size", "true"),
        ("interval_seconds", "-1"),
        ("interval_seconds", "3601"),
        ("interval_seconds", '"5"'),
        ("unknown", "1"),
    ],
)
def test_indexing_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[indexing]\n{field} = {text}\n")
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


def test_body_sample_defaults_follow_d64(tmp_path: Path) -> None:
    body = load_runtime_config(AureusPaths(tmp_path)).body
    assert body.sample_enabled is True
    assert (body.sample_interval_seconds, body.sample_retention_days) == (60.0, 30)


def test_body_sample_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[body]\nsample_enabled = false\nsample_interval_seconds = 5\nsample_retention_days = 7\n",
    )
    body = load_runtime_config(paths).body
    assert body.sample_enabled is False
    assert (body.sample_interval_seconds, body.sample_retention_days) == (5.0, 7)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("sample_interval_seconds", "0"),
        ("sample_interval_seconds", "0.5"),
        ("sample_interval_seconds", "nan"),
        ("sample_interval_seconds", '"60"'),
        ("sample_retention_days", "0"),
        ("sample_retention_days", "-1"),
        ("sample_retention_days", "30.5"),  # 天数必须是整数
        ("sample_enabled", "1"),  # 数字 1 不是 true
    ],
)
def test_body_sample_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[body]\n{field} = {text}\n")
    assert field in error_message_of(paths)


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


def section_lines(lines: list[str], section: str) -> list[str]:
    """模板里某一小节（从 [名字] 到下一个 [ 开头的行之前）的行。

    不同小节可以有同名字段（比如 [web] 和 [feedback] 都有 enabled），
    对照默认值时只能看自己那一节，免得数到别的节里的同名行。
    """
    header = f"[{section}]"
    start = lines.index(header) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    return lines[start:end]


def test_web_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in WEB_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "web")
    assert sum(line.startswith(prefixes) for line in mine) == len(WEB_FIELDS)
    uncommented = [line.removeprefix("# ") if line.startswith(prefixes) else line for line in lines]
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


# ---------- [qq] QQ 通道配置（D136） ----------


def test_qq_allows_all_conversations_by_default(tmp_path: Path) -> None:
    assert QQConfig().allowed_group_id is None
    paths = AureusPaths(tmp_path)
    assert load_runtime_config(paths).qq.allowed_group_id is None
    write_runtime_config(paths, '[qq]\nallowed_group_id = "123456789"\n')
    assert load_runtime_config(paths).qq.allowed_group_id == "123456789"


def test_qq_example_allowed_group_is_optional(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "qq")
    assert '# allowed_group_id = "123456789"' in mine
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, NEWLINE.join(lines))
    assert load_runtime_config(paths).qq.allowed_group_id is None
    uncommented = [
        line.removeprefix("# ") if line.startswith("# allowed_group_id = ") else line
        for line in lines
    ]
    write_runtime_config(paths, NEWLINE.join(uncommented))
    assert load_runtime_config(paths).qq.allowed_group_id == "123456789"


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


# ---------- [feedback] 反馈配置（D67） ----------

FEEDBACK_FIELDS = tuple(FeedbackConfig.model_fields)


def test_feedback_defaults_follow_d67(tmp_path: Path) -> None:
    feedback = load_runtime_config(AureusPaths(tmp_path)).feedback
    assert feedback == FeedbackConfig()
    assert feedback.enabled is True
    assert (feedback.window_size, feedback.min_samples) == (50, 10)


def test_feedback_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in FEEDBACK_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "feedback")
    assert sum(line.startswith(prefixes) for line in mine) == len(FEEDBACK_FIELDS)
    # 只取消 [feedback] 这一节里的注释：别的小节也有 enabled，不能一起取消。
    start = lines.index("[feedback]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(lines))
    assert load_runtime_config(paths).feedback == FeedbackConfig()


def test_feedback_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[feedback]\nenabled = false\nwindow_size = 20\nmin_samples = 5\n")
    feedback = load_runtime_config(paths).feedback
    assert feedback.enabled is False
    assert (feedback.window_size, feedback.min_samples) == (20, 5)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("window_size", "0"),
        ("window_size", "-1"),
        ("window_size", "50.5"),  # 次数必须是整数
        ("window_size", '"50"'),
        ("min_samples", "0"),
        ("min_samples", "true"),
        ("enabled", "1"),  # 数字 1 不是 true
    ],
)
def test_feedback_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[feedback]\n{field} = {text}\n")
    assert field in error_message_of(paths)


def test_feedback_min_samples_cannot_exceed_the_window(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[feedback]\nwindow_size = 5\nmin_samples = 6\n")
    assert "min_samples" in error_message_of(paths)


# ---------- [judge] 判断器官配置（D68） ----------

JUDGE_FIELDS = tuple(JudgeConfig.model_fields)


def test_judge_defaults_follow_d68(tmp_path: Path) -> None:
    judge = load_runtime_config(AureusPaths(tmp_path)).judge
    assert judge == JudgeConfig()
    # 2.3 激素读判断结果，所以默认开；没配 judge 器官时会跳过。
    assert judge.enabled is True
    assert judge.organ == "judge"
    assert (judge.timeout_seconds, judge.context_messages) == (120.0, 5)  # D135


def test_judge_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in JUDGE_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "judge")
    assert sum(line.startswith(prefixes) for line in mine) == len(JUDGE_FIELDS)
    start = lines.index("[judge]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(lines))
    assert load_runtime_config(paths).judge == JudgeConfig()


def test_judge_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[judge]\nenabled = false\ntimeout_seconds = 3\ncontext_messages = 0\n",
    )
    judge = load_runtime_config(paths).judge
    assert judge.enabled is False
    # organ 已固定为 "judge"（D107）
    assert judge.organ == "judge"
    assert (judge.timeout_seconds, judge.context_messages) == (3.0, 0)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("enabled", "1"),  # 数字 1 不是 true
        ("timeout_seconds", "0"),
        ("timeout_seconds", "-1.5"),
        ("timeout_seconds", '"10"'),
        ("timeout_seconds", "true"),
        ("context_messages", "-1"),
        ("context_messages", "2.5"),
        ("context_messages", "true"),
    ],
)
def test_judge_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[judge]\n{field} = {text}\n")
    assert field in error_message_of(paths)


# ---------- [hormones] 激素配置（D69） ----------

NEWLINE = chr(10)  # 拼配置文本用的换行

HORMONES_FIELDS = tuple(HormonesConfig.model_fields)


@pytest.mark.parametrize("switch", ("true", "false"))
def test_legacy_removed_switches_ignored_d107(tmp_path: Path, switch: str) -> None:
    """旧配置里的已删除开关和已固定器官被忽略，不报错（D107）。"""
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[heartbeat]\n"
        f"qq_emoji_semantic_enabled = {switch}\n"
        "[language]\n"
        f"use_vector_expression = {switch}\n"
        'organ = "custom_organ"\n'  # 已固定为 summarizer，应被忽略
        'expression_quality_organ = "custom_judge"\n'  # 已固定为 judge，应被忽略
        'expression_selector_organ = "selector"\n'  # 已固定为 fast_model，应被忽略
        "[judge]\n"
        'organ = "custom_judge"\n'  # 已固定为 judge，应被忽略
        "[exploration]\n"
        'organ = "custom_explorer"\n'  # 已固定为 summarizer，应被忽略
        "[diary]\n"
        "organ = 1\n"  # 即使是旧规则下不合法的值，也不再校验
        "[persona_growth]\n"
        'organ = "custom_growth"\n',  # 已固定为 summarizer，应被忽略
    )
    config = load_runtime_config(paths)
    assert "qq_emoji_semantic_enabled" not in config.heartbeat.model_dump()
    assert "use_vector_expression" not in config.language.model_dump()
    # 所有被删/被固定的字段都取默认值，不受 TOML 中的值影响
    assert config.language.organ == "summarizer"
    assert config.language.expression_quality_organ == "judge"
    assert config.language.expression_selector_organ == "fast_model"
    assert config.judge.organ == "judge"
    assert config.exploration.organ == "summarizer"
    assert config.diary.organ == "summarizer"
    assert config.persona_growth.organ == "summarizer"


def test_expression_vector_candidates_default_and_bounds(tmp_path: Path) -> None:
    """表达向量候选默认 50，范围 1–50（D119）。"""
    assert LanguageConfig().expression_vector_candidates == 50
    paths = AureusPaths(tmp_path)
    for value in ("0", "51"):
        write_runtime_config(paths, f"[language]\nexpression_vector_candidates = {value}\n")
        assert "expression_vector_candidates" in error_message_of(paths)


def test_legacy_reference_chars_is_ignored(tmp_path: Path) -> None:
    """旧配置写了 reference_chars 时照常启动并忽略该键（D120）。"""
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[language]\nreference_chars = 1500\n")
    config = load_runtime_config(paths)
    assert "reference_chars" not in config.language.model_dump()


def test_hormones_defaults_follow_d69(tmp_path: Path) -> None:
    hormones = load_runtime_config(AureusPaths(tmp_path)).hormones
    assert hormones == HormonesConfig()
    assert hormones.enabled is True
    assert hormones.anger_half_life_seconds == 600.0
    assert hormones.surprise_half_life_seconds == 180.0
    assert (hormones.calm_below, hormones.retaliate_at, hormones.cold_shoulder_at) == (
        0.2,
        0.5,
        0.8,
    )


def test_hormones_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in HORMONES_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "hormones")
    assert sum(line.startswith(prefixes) for line in mine) == len(HORMONES_FIELDS)
    start = lines.index("[hormones]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(lines))
    assert load_runtime_config(paths).hormones == HormonesConfig()


def test_hormones_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[hormones]\n"
        "enabled = false\n"
        "anger_enabled = false\n"
        "anger_half_life_seconds = 60\n"
        "max_retaliations = 0\n",
    )
    hormones = load_runtime_config(paths).hormones
    assert hormones.enabled is False
    assert hormones.anger_enabled is False
    assert hormones.anger_half_life_seconds == 60.0
    assert hormones.max_retaliations == 0


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("enabled", "1"),
        ("joy_enabled", '"yes"'),
        ("anger_half_life_seconds", "0"),
        ("hurt_half_life_seconds", "-5"),
        ("surprise_half_life_seconds", '"180"'),
        ("base_amount", "0"),
        ("base_amount", "1.5"),
        ("max_single_rise", "true"),
        ("min_confidence", "-0.1"),
        ("repeat_decay", "0"),
        ("repeat_window_seconds", "0"),
        ("retaliate_at", "0"),
        ("max_retaliations", "-1"),
        ("max_retaliations", "1.5"),
        ("surprise_gap", "2"),
    ],
)
def test_hormones_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[hormones]\n{field} = {text}\n")
    assert field in error_message_of(paths)


@pytest.mark.parametrize(
    "text",
    [
        "calm_below = 0.5\nretaliate_at = 0.5",  # 平息线不低于回击线
        "retaliate_at = 0.9",  # 回击线不低于冷处理线
        "cold_shoulder_at = 0.4",
    ],
)
def test_hormones_levels_must_be_ordered(tmp_path: Path, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[hormones]\n{text}\n")
    assert "calm_below" in error_message_of(paths)


# ---------- [people] 好感配置（D71） ----------

PEOPLE_FIELDS = tuple(PeopleConfig.model_fields)


def test_people_defaults_follow_d71(tmp_path: Path) -> None:
    people = load_runtime_config(AureusPaths(tmp_path)).people
    assert people == PeopleConfig()
    assert people.affinity_enabled is True
    assert (people.user_initial_affinity, people.stranger_affinity) == (80.0, 30.0)
    assert (people.daily_rise_cap, people.daily_fall_cap) == (5.0, 8.0)


def test_people_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in PEOPLE_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "people")
    assert sum(line.startswith(prefixes) for line in mine) == len(PEOPLE_FIELDS)
    start = lines.index("[people]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(lines))
    assert load_runtime_config(paths).people == PeopleConfig()


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("affinity_enabled", "1"),
        ("user_initial_affinity", "0"),
        ("user_initial_affinity", "100"),
        ("stranger_affinity", '"30"'),
        ("praise_amount", "-1"),
        ("daily_fall_cap", "-0.5"),
        ("interaction_amount", "true"),
    ],
)
def test_people_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[people]\n{field} = {text}\n")
    assert field in error_message_of(paths)


# ---------- [empathy] 同理心配置（D72） ----------

EMPATHY_FIELDS = tuple(EmpathyConfig.model_fields)


def test_empathy_defaults_follow_d72(tmp_path: Path) -> None:
    empathy = load_runtime_config(AureusPaths(tmp_path)).empathy
    assert empathy == EmpathyConfig()
    assert (empathy.enabled, empathy.base_amount, empathy.affinity_floor) == (True, 0.3, 0.2)


def test_empathy_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in EMPATHY_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "empathy")
    assert sum(line.startswith(prefixes) for line in mine) == len(EMPATHY_FIELDS)
    start = lines.index("[empathy]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "\n".join(lines))
    assert load_runtime_config(paths).empathy == EmpathyConfig()


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("enabled", "1"),
        ("base_amount", "0"),
        ("base_amount", "1.5"),
        ("affinity_floor", "-0.1"),
        ("affinity_floor", "2"),
        ("affinity_floor", '"0.2"'),
    ],
)
def test_empathy_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[empathy]\n{field} = {text}\n")
    assert field in error_message_of(paths)


# ---------- [mood] 心情配置（D73） ----------

MOOD_FIELDS = tuple(MoodConfig.model_fields)


def test_mood_defaults_follow_d73(tmp_path: Path) -> None:
    mood = load_runtime_config(AureusPaths(tmp_path)).mood
    assert mood == MoodConfig()
    assert mood.enabled is True
    assert mood.half_life_seconds == 21600.0
    assert (mood.cautious_below, mood.recover_above) == (-0.3, -0.15)


def test_mood_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in MOOD_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "mood")
    assert sum(line.startswith(prefixes) for line in mine) == len(MOOD_FIELDS)
    start = lines.index("[mood]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, NEWLINE.join(lines))
    assert load_runtime_config(paths).mood == MoodConfig()


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("enabled", "1"),
        ("half_life_seconds", "0"),
        ("hormone_weight", "-1"),
        ("max_single_change", "0"),
        ("max_single_change", "2"),
        ("stress_weight", "1.5"),
        ("cautious_below", "-2"),
        ("recover_above", '"0"'),
    ],
)
def test_mood_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[mood]{NEWLINE}{field} = {text}{NEWLINE}")
    assert field in error_message_of(paths)


def test_mood_caution_levels_must_be_ordered(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[mood]{NEWLINE}cautious_below = -0.1{NEWLINE}")
    assert "cautious_below" in error_message_of(paths)


# ---------- [agenda] 日程配置（D74） ----------

AGENDA_FIELDS = tuple(AgendaConfig.model_fields)


def test_agenda_defaults_follow_d74(tmp_path: Path) -> None:
    agenda = load_runtime_config(AureusPaths(tmp_path)).agenda
    assert agenda == AgendaConfig()
    assert agenda.enabled is True
    assert (agenda.max_open_per_person, agenda.max_due_days) == (20, 90)
    assert (agenda.expire_grace_days, agenda.context_items) == (3.0, 5)


def test_agenda_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in AGENDA_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "agenda")
    assert sum(line.startswith(prefixes) for line in mine) == len(AGENDA_FIELDS)
    start = lines.index("[agenda]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, NEWLINE.join(lines))
    assert load_runtime_config(paths).agenda == AgendaConfig()


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("enabled", "1"),
        ("max_open_per_person", "0"),
        ("max_open_per_person", "2.5"),
        ("max_due_days", "0"),
        ("expire_grace_days", "-1"),
        ("context_items", '"5"'),
    ],
)
def test_agenda_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[agenda]{NEWLINE}{field} = {text}{NEWLINE}")
    assert field in error_message_of(paths)


# ---------- 社交需求和主动联系配置（D76） ----------

INITIATIVE_FIELDS = tuple(InitiativeConfig.model_fields)


@pytest.mark.parametrize(
    ("section", "config"),
    [("interests", InterestsConfig()), ("exploration", ExplorationConfig())],
)
def test_exploration_config_defaults_and_example(
    tmp_path: Path, section: str, config: InterestsConfig | ExplorationConfig
) -> None:
    assert getattr(load_runtime_config(AureusPaths(tmp_path)), section) == config
    assert config.enabled is False
    lines = Path("config/aureus.example.toml").read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, section)
    prefixes = tuple(f"# {name} =" for name in type(config).model_fields)
    assert sum(line.startswith(prefixes) for line in mine) == len(type(config).model_fields)
    start = lines.index(f"[{section}]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, NEWLINE.join(lines))
    assert getattr(load_runtime_config(paths), section) == config


@pytest.mark.parametrize(
    "text",
    [
        "[exploration]\nenabled = true\n",
        '[exploration]\nsearch_provider = "real"\n',
        "[interests]\nmax_active = 51\n",
        "[interests]\nmax_changes_per_day = -1\n",
        "[exploration]\nenabled = 1\n",
        "[exploration]\nmax_results = 11\n",
        "[exploration]\nfetch_timeout_seconds = 61\n",
        "[exploration]\nmax_page_bytes = 1023\n",
        "[exploration]\nmax_page_chars = 199\n",
        "[exploration]\nshare_within_days = 0\n",
    ],
)
def test_exploration_config_rejects_invalid_values(tmp_path: Path, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, text)
    assert error_message_of(paths)


def test_initiative_defaults_follow_d76(tmp_path: Path) -> None:
    initiative = load_runtime_config(AureusPaths(tmp_path)).initiative
    assert initiative == InitiativeConfig()
    assert initiative.enabled is False
    assert (initiative.max_per_day, initiative.give_up_after) == (2, 3)
    assert (
        initiative.min_interval_hours,
        initiative.max_interval_hours,
        initiative.answer_window_hours,
    ) == (3.0, 48.0, 6.0)


def test_initiative_example_values_equal_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[2] / "config" / "aureus.example.toml"
    prefixes = tuple(f"# {name} =" for name in INITIATIVE_FIELDS)
    lines = example.read_text(encoding="utf-8").splitlines()
    mine = section_lines(lines, "initiative")
    assert sum(line.startswith(prefixes) for line in mine) == len(INITIATIVE_FIELDS)
    start = lines.index("[initiative]") + 1
    for offset, line in enumerate(mine):
        if line.startswith(prefixes):
            lines[start + offset] = line.removeprefix("# ")
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, NEWLINE.join(lines))
    assert load_runtime_config(paths).initiative == InitiativeConfig()


def test_initiative_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[initiative]\nenabled = true\nmax_per_day = 4\nmin_interval_hours = 2\n"
        "max_interval_hours = 24\nanswer_window_hours = 5\ngive_up_after = 2\n",
    )
    assert load_runtime_config(paths).initiative == InitiativeConfig(
        enabled=True,
        max_per_day=4,
        min_interval_hours=2.0,
        max_interval_hours=24.0,
        answer_window_hours=5.0,
        give_up_after=2,
    )


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("enabled", "1"),
        ("max_per_day", "0"),
        ("max_per_day", "true"),
        ("min_interval_hours", "0"),
        ("max_interval_hours", "-1"),
        ("answer_window_hours", "0"),
        ("give_up_after", "0"),
    ],
)
def test_initiative_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[initiative]\n{field} = {text}\n")
    assert field in error_message_of(paths)


def test_initiative_intervals_must_be_ordered(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, "[initiative]\nmin_interval_hours = 49\n")
    assert "min_interval_hours" in error_message_of(paths)


def test_body_social_need_defaults_follow_d76(tmp_path: Path) -> None:
    body = load_runtime_config(AureusPaths(tmp_path)).body
    assert body.social_need_enabled is True
    assert (body.social_need_time_constant_hours, body.social_need_high_at) == (12.0, 0.7)


def test_body_social_need_values_from_file_are_used(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(
        paths,
        "[body]\nsocial_need_enabled = false\nsocial_need_time_constant_hours = 8\n"
        "social_need_high_at = 0.8\n",
    )
    body = load_runtime_config(paths).body
    assert body.social_need_enabled is False
    assert (body.social_need_time_constant_hours, body.social_need_high_at) == (8.0, 0.8)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("social_need_enabled", "1"),
        ("social_need_time_constant_hours", "0"),
        ("social_need_time_constant_hours", '"12"'),
        ("social_need_high_at", "0"),
        ("social_need_high_at", "1.1"),
    ],
)
def test_body_social_need_rejects_bad_values(tmp_path: Path, field: str, text: str) -> None:
    paths = AureusPaths(tmp_path)
    write_runtime_config(paths, f"[body]\n{field} = {text}\n")
    assert field in error_message_of(paths)


def test_old_heartbeat_configuration_uses_thought_deadline_default() -> None:
    runtime = validate_runtime_config({"heartbeat": {"shutdown_grace_seconds": 12}}, "合成配置")
    assert runtime.heartbeat.thought_deadline_seconds == 180


@pytest.mark.parametrize("seconds", [0, 30, 900])
def test_thought_deadline_accepts_disabled_and_range_endpoints(seconds: int) -> None:
    assert HeartbeatConfig(thought_deadline_seconds=seconds).thought_deadline_seconds == seconds


@pytest.mark.parametrize("seconds", [1, 29, 901, -1, float("inf")])
def test_thought_deadline_rejects_invalid_ranges(seconds: float) -> None:
    with pytest.raises(ValueError, match="30～900"):
        HeartbeatConfig(thought_deadline_seconds=seconds)
