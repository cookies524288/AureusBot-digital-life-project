"""主动联系的运行时接线与扮演模式对照，器官全是假实现（D76）。"""

from datetime import timedelta
from pathlib import Path

import pytest

from aureus.common import AureusPaths, ManualClock
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import MessageKind, OutgoingMessage, Perception, TrustLevel
from aureus.organs import (
    CallAttempt,
    CallPurpose,
    KeyResolver,
    OrganBus,
    OrganRequest,
    OrganResponse,
)
from aureus.timeline import TimelineQuery, VisibilityScope
from tests.heartbeat.test_initiative_flow import REACH
from tests.heartbeat.test_judging_flow import NOW, PLAN
from tests.test_roleplay_mode import IDENTITY

pytestmark = pytest.mark.asyncio


class FixedOrgan:
    """固定回规划和回复，经过真网关记账，不连接模型服务。"""

    def __init__(self, organ_id: str, requests: list[OrganRequest]) -> None:
        self.organ_id = organ_id
        self.requests = requests

    async def process(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        text = REACH if request.context.get("task") == "initiative_plan" else PLAN
        if self.organ_id == "replyer":
            text = "好的。"
        return OrganResponse(
            organ_id=self.organ_id,
            suggestion=text,
            attempts=[CallAttempt("fake", "fake", prompt_tokens=2, completion_tokens=1)],
        )


@pytest.mark.parametrize("roleplay", [False, True])
async def test_open_core_wires_initiative_and_disables_it_in_roleplay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roleplay: bool
) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    paths.runtime_config_file.write_text(
        f"[heartbeat]\nroleplay_mode = {str(roleplay).lower()}\n"
        "[body]\nsleep_enabled = false\n[time]\ntimezone = 'UTC'\n"
        "[initiative]\nenabled = true\n[safety]\nmonthly_token_limit = 1400000\n",
        encoding="utf-8",
    )
    requests: list[OrganRequest] = []
    bus = OrganBus()
    bus.register(FixedOrgan("planner", requests))
    bus.register(FixedOrgan("replyer", requests))

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    clock = ManualClock(NOW)
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        sent: list[OutgoingMessage] = []
        core.heartbeat.attach_channel("terminal", sent.append)
        core.heartbeat.wake_up()
        assert core.heartbeat.submit(
            Perception(
                channel="terminal",
                sender_id="local",
                content="你好",
                visibility=VisibilityScope.USER_ONLY,
                trust=TrustLevel.VERIFIED_USER,
            )
        )
        core.heartbeat.beat()
        await core.heartbeat.wait_for_thought()
        core.heartbeat.beat()
        before = len(requests)
        clock.advance(timedelta(hours=20))
        core.heartbeat.beat()
        await core.heartbeat.wait_for_thought()
        core.heartbeat.beat()
        considered = core.timeline.query(TimelineQuery(event_type="initiative_considered"))
        if roleplay:
            assert core.heartbeat._body is None
            assert not core.heartbeat._initiative.enabled
            assert len(requests) == before and not considered
        else:
            assert core.heartbeat._initiative.enabled
            assert considered and considered[-1].metadata["sent"] is True
            assert [request.purpose for request in requests[before:]] == [CallPurpose.OPTIONAL] * 2
            assert len([message for message in sent if message.kind is MessageKind.REPLY]) == 2
        await core.heartbeat.shut_down()
