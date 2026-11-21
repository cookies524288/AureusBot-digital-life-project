"""D81 运行时人设成长的默认开关与扮演模式隔离。"""

from datetime import timedelta
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.common.logging_setup import LOG_FILE_NAME
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.organs import OrganBus, OrganRequest
from aureus.timeline import ActorType, TimelineQuery, VisibilityScope
from tests.heartbeat.test_judging_flow import NOW
from tests.test_exploration_runtime import _paths
from tests.test_initiative_runtime import FixedOrgan

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("enabled, roleplay", [(False, False), (True, True), (True, False)])
async def test_runtime_growth_default_and_roleplay_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, roleplay: bool
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        f"[heartbeat]\nroleplay_mode = {str(roleplay).lower()}\n"
        f"[persona_growth]\nenabled = {str(enabled).lower()}\n"
        "[diary]\nenabled = false\n[time]\ntimezone = 'UTC'\n",
        encoding="utf-8",
    )
    requests: list[OrganRequest] = []
    bus = OrganBus()
    bus.register(FixedOrgan("summarizer", requests))
    monkeypatch.setattr(
        runtime,
        "_build_organ_bus",
        lambda _paths, _heartbeat, resolver, **_kwargs: ModelRuntime(
            bus, resolver, fake_organs=False
        ),
    )
    clock = ManualClock(NOW)
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        core.heartbeat.wake_up()
        core.timeline.append(
            "message", "请多体谅", ActorType.USER, visibility=VisibilityScope.USER_ONLY
        )
        clock.advance(timedelta(hours=10))
        core.heartbeat.beat()
        await core.heartbeat.wait_for_thought()
        core.heartbeat.beat()
        assert bool(requests) is (enabled and not roleplay)
        tracker = core.heartbeat._persona_growth
        assert tracker is not None and tracker.config.enabled is (enabled and not roleplay)
        await core.heartbeat.shut_down()


async def test_missing_persona_organ_logs_once(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        "[persona_growth]\nenabled = true\n[diary]\nenabled = false\n", encoding="utf-8"
    )
    with runtime.open_core(
        paths, fake_organs=True, clock=ManualClock(NOW), to_console=False
    ) as core:
        core.heartbeat.wake_up()
        for _ in range(3):
            core.heartbeat.beat()
        assert not core.timeline.query(TimelineQuery(event_type="persona_changed"))
        await core.heartbeat.shut_down()
    assert (paths.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8").count(
        "没有总结器官，不做人设回顾"
    ) == 1
