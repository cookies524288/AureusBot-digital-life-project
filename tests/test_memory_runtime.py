"""运行时的记忆开关、建表失败和扮演模式日记接线（D80）。"""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.common.logging_setup import LOG_FILE_NAME
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.organs import OrganBus
from aureus.timeline import ActorType
from tests.heartbeat.test_judging_flow import NOW
from tests.test_exploration_runtime import _paths
from tests.test_initiative_runtime import FixedOrgan

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("roleplay", [False, True])
async def test_runtime_opens_memory_and_diary_in_roleplay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    roleplay: bool,
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        f"[heartbeat]\nroleplay_mode = {str(roleplay).lower()}\n"
        "[diary]\nwrite_after_hour = 22\n[time]\ntimezone = 'UTC'\n",
        encoding="utf-8",
    )
    requests = []
    bus = OrganBus()
    for organ_id in ("planner", "replyer", "summarizer"):
        bus.register(FixedOrgan(organ_id, requests))
    monkeypatch.setattr(
        runtime,
        "_build_organ_bus",
        lambda _paths, _heartbeat, resolver, **_kwargs: ModelRuntime(
            bus, resolver, fake_organs=False
        ),
    )
    clock = ManualClock(NOW)
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        index = core.heartbeat._memory.index
        assert index is not None
        core.heartbeat.wake_up()
        original = core.timeline.append("message", "曾经说过木卫二", ActorType.USER)
        experience = core.timeline.append("diary_written", "记录木卫二观测计划", ActorType.ME)
        clock.advance(timedelta(hours=10))
        await core.heartbeat.beat()
        assert [hit.record_id for hit in index.search(["木卫二"], (), 5)] == [experience.id]
        assert core.timeline.get(original.id) == original
        await core.heartbeat.wait_for_thought()
        await core.heartbeat.beat()
        assert (paths.diary_dir / "2026-11-17.md").is_file()
        await core.heartbeat.shut_down()


async def test_disabled_memory_does_not_open_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text("[memory]\nenabled = false\n", encoding="utf-8")

    def forbidden(_self: runtime.MemoryIndex) -> None:
        raise AssertionError("关闭时不应建索引")

    monkeypatch.setattr(runtime.MemoryIndex, "open", forbidden)
    with runtime.open_core(
        paths, fake_organs=True, clock=ManualClock(NOW), to_console=False
    ) as core:
        assert core.heartbeat._memory.index is None
        assert not core.heartbeat._conversation._can_recall()


async def test_index_open_failure_disables_recall_and_core_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)

    def broken(_self: runtime.MemoryIndex) -> None:
        raise sqlite3.OperationalError("假不支持 trigram")

    monkeypatch.setattr(runtime.MemoryIndex, "open", broken)
    with runtime.open_core(
        paths, fake_organs=True, clock=ManualClock(NOW), to_console=False
    ) as core:
        core.heartbeat.wake_up()
        await core.heartbeat.beat()
        assert core.heartbeat._memory.index is None
        assert not core.heartbeat._conversation._can_recall()
        await core.heartbeat.shut_down()
    text = (paths.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert text.count("记忆索引打不开，本次运行关闭记忆") == 1


async def test_missing_diary_organ_logs_once_at_startup(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    clock = ManualClock(NOW)
    with runtime.open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        core.heartbeat.wake_up()
        clock.advance(timedelta(hours=10))
        for _ in range(3):
            await core.heartbeat.beat()
        assert not core.heartbeat.is_thinking
        await core.heartbeat.shut_down()
    text = (paths.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert text.count("没有总结器官，不写日记") == 1
