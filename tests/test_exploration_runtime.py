"""运行时同步兴趣、关闭扮演模式探索；不连接真实搜索或模型（D79）。"""

from pathlib import Path

import pytest

from aureus.agenda import InterestStore
from aureus.common import AureusPaths, ManualClock
from aureus.common.logging_setup import LOG_FILE_NAME
from aureus.core import runtime
from aureus.organs import OrganBus
from tests.heartbeat.test_judging_flow import NOW
from tests.test_initiative_runtime import FixedOrgan
from tests.test_roleplay_mode import IDENTITY

pytestmark = pytest.mark.asyncio


def _paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY + '\ninterests = ["天文"]\n', encoding="utf-8")
    return paths


async def test_runtime_syncs_seeds_even_when_interests_disabled(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    clock = ManualClock(NOW)
    with runtime.open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        store = core.heartbeat._exploration.interests
        assert store is not None and not store.config.enabled
        assert [i.text for i in store.active()] == ["天文"]
    paths.identity_file.write_text(IDENTITY + '\ninterests = ["音乐"]\n', encoding="utf-8")
    with runtime.open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        store = core.heartbeat._exploration.interests
        assert store is not None and [i.text for i in store.active()] == ["音乐"]
        old = store.get(1)
        assert old is not None and old.end_reason == "user_removed"
        store.remove(2, "user_removed")
    with runtime.open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        store = core.heartbeat._exploration.interests
        assert store is not None and not store.active()


@pytest.mark.parametrize("roleplay", [False, True])
async def test_runtime_none_search_skips_once_and_roleplay_disables_exploration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roleplay: bool
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        f"[heartbeat]\nroleplay_mode = {str(roleplay).lower()}\n"
        "[interests]\nenabled = true\n[exploration]\nenabled = true\n"
        "[body]\nsleep_enabled = false\n",
        encoding="utf-8",
    )
    requests = []
    bus = OrganBus()
    for organ_id in ("planner", "replyer", "summarizer"):
        bus.register(FixedOrgan(organ_id, requests))
    monkeypatch.setattr(runtime, "_build_organ_bus", lambda *_args, **_kwargs: bus)
    with runtime.open_core(paths, clock=ManualClock(NOW), to_console=False) as core:
        tracker = core.heartbeat._exploration
        assert tracker.search is None and tracker.config.enabled is not roleplay
        assert tracker.has_organ
        core.heartbeat.wake_up()
        for _ in range(3):
            core.heartbeat.beat()
        assert not core.heartbeat.is_thinking and not requests
        if roleplay:
            assert tracker.interests is None
        await core.heartbeat.shut_down()
    logs = (paths.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert logs.count("没配搜索服务，不探索") == (0 if roleplay else 1)


async def test_seed_sync_failure_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("假同步故障")

    monkeypatch.setattr(InterestStore, "sync_seeds", broken)
    with runtime.open_core(
        paths, fake_organs=True, clock=ManualClock(NOW), to_console=False
    ) as core:
        assert core.heartbeat._exploration.interests is not None
