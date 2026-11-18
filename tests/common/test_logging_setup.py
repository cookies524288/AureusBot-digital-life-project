"""日志的测试：写文件、级别过滤、密钥不出现在日志里。"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from aureus.common import (
    AureusPaths,
    configure_logging,
    configure_logging_for,
    load_settings,
    shutdown_logging,
)
from aureus.common.logging_setup import LOG_FILE_NAME, REDACTED

SECRET = "sk-test-0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("AUREUS_LLM_API_KEY", raising=False)
    yield
    shutdown_logging()


def read_log(log_dir: Path) -> str:
    shutdown_logging()  # 先关掉，保证排队的日志都已经写进文件
    return (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")


def test_messages_are_written_to_file(tmp_path: Path) -> None:
    configure_logging(tmp_path, "INFO", to_console=False)
    logger.info("你好，日志")
    assert "你好，日志" in read_log(tmp_path)


def test_log_directory_is_created(tmp_path: Path) -> None:
    log_dir = tmp_path / "nested" / "logs"
    configure_logging(log_dir, "INFO", to_console=False)
    logger.info("x")
    read_log(log_dir)
    assert log_dir.is_dir()


def test_level_filters_lower_levels(tmp_path: Path) -> None:
    configure_logging(tmp_path, "WARNING", to_console=False)
    logger.info("不该出现")
    logger.warning("应该出现")
    text = read_log(tmp_path)
    assert "不该出现" not in text
    assert "应该出现" in text


def test_secret_in_message_is_hidden(tmp_path: Path) -> None:
    configure_logging(tmp_path, "INFO", [SECRET], to_console=False)
    logger.info("拿到密钥 {}", SECRET)
    logger.info(f"拼接的消息 {SECRET}")
    text = read_log(tmp_path)
    assert SECRET not in text
    assert text.count(REDACTED) == 2


def test_secret_in_exception_traceback_is_hidden(tmp_path: Path) -> None:
    configure_logging(tmp_path, "INFO", [SECRET], to_console=False)
    try:
        raise RuntimeError(f"认证失败：{SECRET}")
    except RuntimeError:
        logger.exception("调用失败")
    text = read_log(tmp_path)
    assert "调用失败" in text
    assert "RuntimeError" in text
    assert SECRET not in text
    assert REDACTED in text


def test_longer_secret_is_replaced_before_the_shorter_one_inside_it(tmp_path: Path) -> None:
    short = "abcdef123456"
    long = short + "-tail-zzzz"
    configure_logging(tmp_path, "INFO", [short, long], to_console=False)
    logger.info(f"值是 {long}")
    text = read_log(tmp_path)
    assert "tail-zzzz" not in text
    assert short not in text


def test_empty_secret_is_ignored(tmp_path: Path) -> None:
    configure_logging(tmp_path, "INFO", [""], to_console=False)
    logger.info("正常消息")
    assert "正常消息" in read_log(tmp_path)


def test_reconfiguring_does_not_duplicate_lines(tmp_path: Path) -> None:
    configure_logging(tmp_path, "INFO", to_console=False)
    configure_logging(tmp_path, "INFO", to_console=False)
    logger.info("只写一次的消息")
    assert read_log(tmp_path).count("只写一次的消息") == 1


def test_configure_logging_for_settings_hides_loaded_secrets(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    paths.env_file.write_text(f"AUREUS_LLM_API_KEY={SECRET}\n", encoding="utf-8")
    configure_logging_for(load_settings(paths), to_console=False)
    logger.info(f"密钥是 {SECRET}")
    text = read_log(paths.log_dir)
    assert SECRET not in text
    assert REDACTED in text


def test_stdlib_logging_reaches_the_file_and_is_redacted(tmp_path: Path) -> None:
    # 心跳、网关、时间线用的是标准库 logging：它们的日志也要进文件、也要过密钥过滤。
    configure_logging(tmp_path, "INFO", [SECRET], to_console=False)
    logging.getLogger("aureus.safety.gateway").warning("标准库日志，密钥 %s", SECRET)
    text = read_log(tmp_path)
    assert "标准库日志" in text
    assert SECRET not in text
    assert REDACTED in text


def test_stdlib_log_points_at_the_real_caller(tmp_path: Path) -> None:
    configure_logging(tmp_path, "INFO", to_console=False)
    logging.getLogger("aureus.heartbeat").warning("看看是谁打的")
    text = read_log(tmp_path)
    line = next(line for line in text.splitlines() if "看看是谁打的" in line)
    assert "test_stdlib_log_points_at_the_real_caller" in line


def test_stdlib_level_is_respected(tmp_path: Path) -> None:
    configure_logging(tmp_path, "WARNING", to_console=False)
    logging.getLogger("aureus.timeline").info("不该出现")
    logging.getLogger("aureus.timeline").error("应该出现")
    text = read_log(tmp_path)
    assert "不该出现" not in text
    assert "应该出现" in text


def test_shutdown_removes_the_bridge_and_restores_the_root_level(tmp_path: Path) -> None:
    root = logging.getLogger()
    level_before = root.level
    handlers_before = list(root.handlers)
    configure_logging(tmp_path, "DEBUG", to_console=False)
    configure_logging(tmp_path, "INFO", to_console=False)  # 重复调用不叠加处理器
    assert len(root.handlers) == len(handlers_before) + 1
    shutdown_logging()
    assert root.handlers == handlers_before
    assert root.level == level_before
