"""网页服务组装的测试：开关、接到心跳上、默认密码下不开放局域网（路线图 1.10，D63 第 6、8 条）。"""

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from aureus.channels import WEB_CHANNEL_NAME
from aureus.common import AureusPaths, ConfigError, ManualClock, shutdown_logging
from aureus.core.runtime import open_core
from aureus.web import PasswordStore
from aureus.web.assemble import open_web
from aureus.web.runner import WebServer

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

IDENTITY = textwrap.dedent(
    """\
    [facts]
    name = "测试豆"
    birthday = "2008-01-01"

    [user]
    name = "测试用户"

    [persona]
    immutable = "你是一个测试用的数字生命。"
    """
)


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    return paths


def write_web_config(paths: AureusPaths, text: str) -> None:
    paths.runtime_config_file.write_text(f"[web]\n{text}", encoding="utf-8")


def test_a_disabled_web_channel_assembles_nothing(paths: AureusPaths) -> None:
    write_web_config(paths, "enabled = false\n")
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        with open_web(core, clock) as server:
            assert server is None
        # 没有接过网页通道：现在还能接。
        core.heartbeat.attach_channel(WEB_CHANNEL_NAME, lambda _message: None)


def test_an_enabled_web_channel_is_attached_to_the_heartbeat(paths: AureusPaths) -> None:
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert isinstance(server, WebServer)
        assert server.started is False  # 只是组装好，监听由调用方启动
        with pytest.raises(ValueError, match="已经接上"):
            core.heartbeat.attach_channel(WEB_CHANNEL_NAME, lambda _message: None)
    # 关闭后数据库连接都放掉了：Windows 上文件没被锁住，能删掉。
    paths.database_file.unlink()


def test_remote_access_with_the_default_password_refuses_to_assemble(paths: AureusPaths) -> None:
    write_web_config(paths, "allow_remote = true\n")
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        pytest.raises(ConfigError, match="allow_remote"),
        open_web(core, clock),
    ):
        pass


def test_remote_access_is_allowed_once_the_password_was_changed(paths: AureusPaths) -> None:
    PasswordStore(paths.web_password_file, FAST_HASHER).set_password("correct horse")
    write_web_config(paths, "allow_remote = true\n")
    clock = ManualClock(START)
    with (
        open_core(paths, fake_organs=True, clock=clock, to_console=False) as core,
        open_web(core, clock) as server,
    ):
        assert isinstance(server, WebServer)
