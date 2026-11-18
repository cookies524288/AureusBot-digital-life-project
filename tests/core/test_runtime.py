"""核心组装的测试：用假器官和临时数据根目录，不调用模型、不碰真实的数据。"""

import logging
import os
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import AureusPaths, ConfigError, ManualClock, shutdown_logging
from aureus.common.logging_setup import LOG_FILE_NAME
from aureus.common.vault import VaultError
from aureus.core.runtime import open_core
from aureus.heartbeat import EVENT_SHUT_DOWN, EVENT_WOKE_UP
from aureus.identity import IdentityFileNotFoundError, SingleInstanceLockError
from aureus.timeline import TimelineQuery

START = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
FAKE_SECRET = "sk-fake-runtime-0001"

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


def test_assembles_with_fake_organs_and_closes_cleanly(paths: AureusPaths) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        assert core.identity.facts.name == "测试豆"
        assert core.heartbeat.downtime is None  # 组装好了，但还没有醒来
        core.heartbeat.wake_up()
        assert core.timeline.count_records() == 1
    # 退出 with 块后数据库已经关掉：Windows 上文件没被锁住，能删掉。
    paths.database_file.unlink()


def test_second_instance_is_refused(paths: AureusPaths) -> None:
    with (
        open_core(paths, fake_organs=True, to_console=False),
        pytest.raises(SingleInstanceLockError),
        open_core(paths, fake_organs=True, to_console=False),
    ):
        pass


def test_lock_is_released_after_closing(paths: AureusPaths) -> None:
    with open_core(paths, fake_organs=True, to_console=False):
        pass
    with open_core(paths, fake_organs=True, to_console=False):
        pass


def test_missing_identity_is_reported(paths: AureusPaths) -> None:
    paths.identity_file.unlink()
    with (
        pytest.raises(IdentityFileNotFoundError),
        open_core(paths, fake_organs=True, to_console=False),
    ):
        pass


def test_real_organs_need_models_config(paths: AureusPaths) -> None:
    # 不是假器官模式时，没有 models.toml 就在启动时报错，不等到第一句话。
    with pytest.raises(ConfigError), open_core(paths, to_console=False):
        pass


def test_reply_organ_must_be_configured(paths: AureusPaths) -> None:
    paths.models_config_file.write_text(
        textwrap.dedent(
            """\
            [[providers]]
            name = "fake"
            protocol = "openai"
            base_url = "http://127.0.0.1:9"
            api_key_env = "AUREUS_KEY_FAKE"

            [[models]]
            name = "fake-model"
            provider = "fake"
            identifier = "fake-model"

            [organs.planner]
            models = ["fake-model"]
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="replyer"), open_core(paths, to_console=False):
        pass


def test_unreadable_vault_stops_startup(paths: AureusPaths) -> None:
    # 密钥库在、主密钥不在：报错停下，不回退到明文 .env（D36）。
    paths.vault_file.write_text('{"format_version": 1, "token": "x"}', encoding="utf-8")
    with pytest.raises(VaultError), open_core(paths, fake_organs=True, to_console=False):
        pass


def test_dotenv_keys_are_hidden_from_logs(paths: AureusPaths) -> None:
    paths.env_file.write_text(f"AUREUS_KEY_FAKE={FAKE_SECRET}\n", encoding="utf-8")
    with open_core(paths, fake_organs=True, to_console=False):
        logging.getLogger("aureus.test").warning("不小心打出了 %s", FAKE_SECRET)
    shutdown_logging()
    text = (paths.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "不小心打出了" in text
    assert FAKE_SECRET not in text
    # 明文警告只写名字，不写值。
    assert "AUREUS_KEY_FAKE" in text


@pytest.mark.asyncio
async def test_restart_reports_downtime(paths: AureusPaths) -> None:
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        core.heartbeat.wake_up()
        await core.heartbeat.shut_down()
    clock.advance(timedelta(hours=1))
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        downtime = core.heartbeat.wake_up()
        records = core.timeline.query(TimelineQuery())
    assert [record.event_type for record in records] == [
        EVENT_WOKE_UP,
        EVENT_SHUT_DOWN,
        EVENT_WOKE_UP,
    ]
    assert "1 小时" in downtime.describe()


def test_startup_prunes_snapshots_over_the_limit(paths: AureusPaths) -> None:
    paths.runtime_config_file.write_text("[snapshots]\nmax_total_megabytes = 1\n", encoding="utf-8")
    old = paths.snapshots_dir / "old.zip"
    new = paths.snapshots_dir / "new.zip"
    old.write_bytes(b"x" * (1024 * 1024))
    new.write_bytes(b"x" * (1024 * 1024))
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False):
        pass
    assert not old.exists()
    assert new.exists()
