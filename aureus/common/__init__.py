"""公共地基 common：真正通用的小工具（时钟、数据根目录、配置、日志）。

本包不依赖任何子系统，见 docs/11-dev-guide.md 第 2.2 节。
"""

from aureus.common.clock import AcceleratedClock, Clock, ManualClock, SystemClock
from aureus.common.config import (
    BiomodelConfig,
    ConfigError,
    HeartbeatConfig,
    RuntimeConfig,
    SafetyConfig,
    Settings,
    SnapshotsConfig,
    load_runtime_config,
    load_settings,
)
from aureus.common.logging_setup import configure_logging, shutdown_logging
from aureus.common.paths import AureusPaths, HomePathError, home_is_default, resolve_home

__all__ = [
    "AcceleratedClock",
    "AureusPaths",
    "BiomodelConfig",
    "Clock",
    "ConfigError",
    "HeartbeatConfig",
    "HomePathError",
    "ManualClock",
    "RuntimeConfig",
    "SafetyConfig",
    "Settings",
    "SnapshotsConfig",
    "SystemClock",
    "configure_logging",
    "home_is_default",
    "load_runtime_config",
    "load_settings",
    "resolve_home",
    "shutdown_logging",
]
