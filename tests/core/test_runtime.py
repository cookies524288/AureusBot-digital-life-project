"""核心组装的测试：用假器官和临时数据根目录，不调用模型、不碰真实的数据。"""

import logging
import os
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.attention.working_memory import CHECKPOINT
from aureus.common import AureusPaths, ConfigError, ManualClock, shutdown_logging
from aureus.common.logging_setup import LOG_FILE_NAME
from aureus.common.vault import VaultError
from aureus.core.runtime import open_core
from aureus.heartbeat import EVENT_IDENTITY_CHANGED, EVENT_SHUT_DOWN, EVENT_WOKE_UP
from aureus.identity import IdentityFileNotFoundError, IdentityLayer, SingleInstanceLockError
from aureus.timeline import TimelineQuery

START = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
FAKE_SECRET = "sk-fake-runtime-0001"

IDENTITY = textwrap.dedent(
    """\
    [facts]
    name = "测试助手"
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


@pytest.mark.asyncio
async def test_assembles_with_fake_organs_and_closes_cleanly(paths: AureusPaths) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        assert core.identity.facts.name == "测试助手"
        assert core.heartbeat.downtime is None  # 组装好了，但还没有醒来
        core.heartbeat.wake_up()
        await core.heartbeat.shut_down()
        assert [record.event_type for record in core.timeline.query(TimelineQuery())] == [
            CHECKPOINT,
            EVENT_WOKE_UP,
            EVENT_SHUT_DOWN,
        ]
    # 退出 with 块后数据库已经关掉：Windows 上文件没被锁住，能删掉。
    paths.database_file.unlink()


@pytest.mark.asyncio
async def test_periodic_emoji_maintenance_refreshes_real_candidates(paths: AureusPaths) -> None:
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        store = core.emoji_store
        assert store is not None
        image = b"GIF89a synthetic media"
        entry = store.collect(image, description="表情", register=True)
        cancelled = store.collect(b"GIF89a manual cancellation", description="取消", register=True)
        assert entry.digest is not None and cancelled.digest is not None
        assert store.update_catalog(cancelled.digest, is_registered=False)
        attachment = paths.memory_dir / "attachments" / entry.digest
        attachment.write_bytes(b"GIF89a wrong checksum")

        async def advance_maintenance() -> None:
            clock.advance(timedelta(minutes=10))
            await core.heartbeat.beat()
            maintenance = core.heartbeat._emoji_maintenance
            assert maintenance is not None
            await maintenance.wait()

        core.heartbeat.wake_up()
        await core.heartbeat.beat()
        assert [item.digest for item in store.candidates()] == [entry.digest]
        await advance_maintenance()
        assert not store.candidates()
        assert attachment.read_bytes() == b"GIF89a wrong checksum"

        attachment.write_bytes(image)
        assert store.preview_integrity(30).unavailable == 0
        assert not store.candidates()
        await advance_maintenance()
        assert [item.digest for item in store.candidates()] == [entry.digest]
        assert store.read_sendable(entry.digest) == image
        assert not store.browse("取消", 0, 1)[0].is_registered
        await core.heartbeat.shut_down()


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
        CHECKPOINT,
        EVENT_WOKE_UP,
        EVENT_SHUT_DOWN,
        EVENT_WOKE_UP,
    ]
    assert downtime.shutdown_recorded
    assert downtime.duration == timedelta(hours=1)


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


def test_the_user_and_the_terminal_account_are_registered(paths: AureusPaths) -> None:
    paths.identity_file.write_text(
        IDENTITY.replace(
            'name = "测试用户"', 'name = "测试用户"\naccounts = [{ platform = "qq", id = "10000" }]'
        ),
        encoding="utf-8",
    )
    with open_core(paths, fake_organs=True, to_console=False) as core:
        user = core.people.find("terminal", "local")
        assert user is not None and user.is_user
        assert user.name == "测试用户"
        same = core.people.find("qq", "10000")
        assert same is not None and same.id == user.id


def test_an_account_owned_by_someone_else_stops_startup(paths: AureusPaths) -> None:
    with open_core(paths, fake_organs=True, to_console=False) as core:
        core.people.meet("qq", "10000")
    paths.identity_file.write_text(
        IDENTITY.replace(
            'name = "测试用户"', 'name = "测试用户"\naccounts = [{ platform = "qq", id = "10000" }]'
        ),
        encoding="utf-8",
    )
    with (
        pytest.raises(ConfigError, match="已经登记在别人名下"),
        open_core(paths, fake_organs=True, to_console=False),
    ):
        pass


@pytest.mark.asyncio
async def test_editing_the_persona_is_remembered_across_restarts(paths: AureusPaths) -> None:
    """路线图 1.9 的验收：改配置后重启，时间线有「用户修改了我的设定」，历史能看到旧版本。"""
    with_style = IDENTITY + 'expression_style = "句子短。"\n'
    paths.identity_file.write_text(with_style, encoding="utf-8")
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        core.heartbeat.wake_up()
        await core.heartbeat.shut_down()

    paths.identity_file.write_text(with_style.replace("句子短。", "多用语气词。"), encoding="utf-8")
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        assert core.identity.persona.expression_style == "多用语气词。"
        core.heartbeat.wake_up()
        changes = core.timeline.query(TimelineQuery(event_type=EVENT_IDENTITY_CHANGED))
        old = core.identity_history.versions(IdentityLayer.EXPRESSION_STYLE, limit=10)

    assert [record.content for record in changes] == ["用户修改了我的设定：表达风格。"]
    assert [version.content for version in old] == ["句子短。", "多用语气词。"]


def test_the_web_account_is_registered_as_the_same_user_as_the_terminal(
    paths: AureusPaths,
) -> None:
    # 网页要密码登录，能走到心跳的网页消息一定是用户本人（D63 第 8 条）：
    # 终端和网页是同一个人，所以共用同一份档案、同一个注意力。
    with open_core(paths, fake_organs=True, to_console=False) as core:
        terminal = core.people.find("terminal", "local")
        web = core.people.find("web", "local")
        assert terminal is not None and web is not None
        assert web.is_user
        assert web.id == terminal.id
