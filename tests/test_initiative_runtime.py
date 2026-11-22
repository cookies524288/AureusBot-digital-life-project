"""主动联系的运行时接线与扮演模式对照，器官全是假实现（D76）。"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import AureusPaths, ManualClock
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.feedback import COMMITMENT_KEPT, Outcome
from aureus.heartbeat import MessageKind, OutgoingMessage, Perception, TrustLevel
from aureus.heartbeat.initiative import InitiativeDecision
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


@pytest.mark.parametrize(
    ("enabled", "scenario", "threshold", "delay_minutes"),
    [
        (False, "morning", None, 0),
        (True, "morning", 0.65, 73),
        (True, "morning", None, 43),
        (True, "morning", 0.85, 20),
        (False, "evening", None, 0),
        (True, "evening", None, 0),
        (False, "pending", None, 0),
        (True, "pending", None, 0),
        (False, "due", None, 0),
        (True, "due", None, 0),
    ],
)
async def test_cortisol_only_defers_optional_contact_after_real_agenda_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    scenario: str,
    threshold: float | None,
    delay_minutes: int,
) -> None:
    zone = "Etc/GMT-12" if scenario == "evening" else "UTC"
    threshold_line = f"defer_at = {threshold}\n" if threshold is not None else ""
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    paths.runtime_config_file.write_text(
        "[body]\nsleep_enabled = false\nsocial_need_high_at = 0.818\n"
        f"[agenda]\nexpire_grace_days = 1.0\n[time]\ntimezone = '{zone}'\n"
        "[initiative]\nenabled = true\n[mood]\nenabled = false\n"
        "[safety]\nmonthly_token_limit = 1400000\n"
        f"[cortisol]\nenabled = {str(enabled).lower()}\n{threshold_line}",
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
    delivered: list[datetime] = []

    def receive(outgoing: OutgoingMessage) -> None:
        if outgoing.kind is MessageKind.REPLY:
            delivered.append(clock.now())

    message = Perception(
        channel="terminal",
        sender_id="local",
        content="这周有人参加活动吗？",
        visibility=VisibilityScope.GROUP,
        trust=TrustLevel.VERIFIED_USER,
    )
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        heartbeat = core.heartbeat
        heartbeat.attach_channel("terminal", receive)
        heartbeat.wake_up()
        assert heartbeat.submit(message)
        heartbeat.beat()
        await heartbeat.wait_for_thought()
        heartbeat.beat()
        source = core.timeline.query(TimelineQuery(event_type="message"))[0]
        user = core.people.find("terminal", "local")
        agenda = heartbeat._agenda
        feedback = core.feedback
        assert user is not None and agenda is not None and feedback is not None
        record_id = feedback.record_expectation(COMMITMENT_KEPT, 0.8, source.id)
        settled_at = NOW + timedelta(hours=20, minutes=22)
        candidate_at = settled_at + timedelta(minutes=10)
        due_at = settled_at - timedelta(days=1, seconds=1)
        if scenario == "due":
            due_at = candidate_at
        elif scenario == "pending":
            due_at = candidate_at + timedelta(days=2)
        assert (
            agenda.add(
                user.id,
                "测试承诺",
                due_at=due_at,
                source_record_id=source.id,
                feedback_id=record_id,
            )
            is not None
        )
        clock.advance(settled_at - clock.now())
        heartbeat.beat()
        result = feedback.get(record_id)
        assert result is not None and result.source_id == source.id
        failed = scenario in {"morning", "evening"}
        assert result.outcome is (Outcome.FAILURE if failed else Outcome.UNKNOWN)
        assert result.outcome_at == (settled_at if failed else None)
        assert not heartbeat.is_thinking

        clock.advance(timedelta(minutes=10))
        peak = clock.now()
        timing = heartbeat._cortisol
        level_at_peak = timing.level(peak) if timing is not None else None
        before = len(requests)
        assert heartbeat._hormone_levels() == dict(anger=0.0, joy=0.0, hurt=0.0, surprise=0.0)
        assert delivered == [NOW]
        delivered.clear()
        checks: list[datetime] = []
        original_consider = heartbeat._initiative.consider

        def observe(decision: InitiativeDecision) -> bool:
            if decision.allowed or decision.blocked_by == "皮质醇调节读数达到延后门槛":
                checks.append(clock.now())
            return original_consider(decision)

        monkeypatch.setattr(heartbeat._initiative, "consider", observe)
        for _ in range(delay_minutes + 1):
            heartbeat.beat()
            if heartbeat.is_thinking:
                break
            assert heartbeat._initiative.state.last_considered_at is None
            assert len(requests) == before and not delivered
            clock.advance(timedelta(minutes=1))
        assert heartbeat.is_thinking
        release = peak + timedelta(minutes=delay_minutes)
        assert checks == [peak + timedelta(minutes=m) for m in range(delay_minutes + 1)]
        assert heartbeat._initiative.state.last_considered_at == release
        await heartbeat.wait_for_thought()
        heartbeat.beat()
        assert delivered == [release]
        assert [r.context.get("task") for r in requests[before:]] == [
            "initiative_plan",
            "initiative_reply",
        ]
        assert all(r.purpose is CallPurpose.OPTIONAL for r in requests[before:])
        [considered] = core.timeline.query(TimelineQuery(event_type="initiative_considered"))
        assert considered.metadata["sent"] is True
        assert bool(considered.metadata["due_item_ids"]) == (scenario == "due")
        await heartbeat.shut_down()

    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        rebuilt = core.heartbeat._cortisol
        assert (rebuilt.level(peak) if rebuilt is not None else None) == level_at_peak
