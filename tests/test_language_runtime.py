"""用假器官沿入站、规划、回复和重启路径验证 D89 的习得语言。"""

import json
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import Perception, TrustLevel
from aureus.memory import LanguageKind, LanguageStore
from aureus.organs import CallPurpose, KeyResolver, OrganBus, OrganRequest, OrganResponse
from aureus.timeline import ActorType, TimelineQuery, VisibilityScope
from tests.heartbeat.test_judging_flow import NOW
from tests.test_exploration_runtime import _paths


class LanguageOrgan:
    """按任务提供合成提议，并保留实际收到的提示。"""

    def __init__(self, organ_id: str, requests: list[OrganRequest]) -> None:
        self.organ_id = organ_id
        self.requests = requests

    async def process(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        task = request.context["task"]
        if task == "language_learning":
            ids = request.context["source_ids"]
            proposals = [
                {"kind": "jargon", "text": "GG", "detail": detail, "ids": ids[start:end]}
                for detail, start, end in (("抵达终点", 0, 4), ("等候开局", 4, 8))
            ]
            proposals.extend(
                [
                    {
                        "kind": "expression",
                        "text": "月兔冲线",
                        "detail": "庆祝跑步成绩",
                        "ids": ids,
                    },
                    {"kind": "expression", "text": "凭空造词", "detail": "跑步", "ids": [*ids, -1]},
                ]
            )
            text = json.dumps({"proposals": proposals}, ensure_ascii=False)
        elif task == "plan":
            text = '{"reply":true,"intent":"回应消息","expression_intent":{"scene":"庆祝跑步成绩"}}'
            if "陌生主题" in request.prompt:
                text = '{"reply":true,"intent":"问好"}'
        else:
            text = "收到。"
        return OrganResponse(organ_id=self.organ_id, suggestion=text)


@pytest.mark.asyncio
@pytest.mark.parametrize(("learning", "budget"), [(True, 700), (False, 700), (True, 0)])
async def test_language_normal_inbound_across_group_and_private_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, learning: bool, budget: int
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        "[heartbeat]\nplanning_enabled = true\n"
        "[body]\nsleep_enabled = false\n[judge]\nenabled = false\n"
        "[language]\n"
        f"learning_enabled = {str(learning).lower()}\nreference_chars = {budget}\n",
        encoding="utf-8",
    )
    requests: list[OrganRequest] = []
    bus = OrganBus()
    for organ in ("planner", "replyer", "summarizer"):
        bus.register(LanguageOrgan(organ, requests))

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    store = LanguageStore(paths.database_file)
    samples = [
        Perception(
            channel="mock_group_a" if index < 4 else "mock_group_b",
            sender_id="runner",
            content=(
                f"跑步第 {index} 场：GG，月兔冲线。"
                if index < 4
                else f"集合第 {index} 场：gg，等开局。"
            ),
            visibility=VisibilityScope.GROUP,
            trust=TrustLevel.UNVERIFIED,
            source_message_id=f"sample-{index}",
        )
        for index in range(8)
    ]

    with runtime.open_core(paths, clock=ManualClock(NOW), to_console=False) as core:
        heart = core.heartbeat
        for channel in ("mock_group_a", "mock_group_b"):
            heart.attach_channel(channel, lambda _message: None)
        heart.wake_up()
        core.timeline.append("message", "历史回放，无入站", ActorType.USER)
        for sample in [*samples, samples[0]]:
            assert heart.submit(sample)
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        entries = store.entries(LanguageKind.EXPRESSION)
        assert [(entry.text, entry.count) for entry in entries] == (
            [("月兔冲线", 4)] if learning else []
        )
        jargon = store.entries(LanguageKind.JARGON)
        assert [(entry.detail, entry.count) for entry in jargon] == (
            [("抵达终点", 4), ("等候开局", 4)] if learning else []
        )
        await heart.shut_down()

    if learning:
        request = requests[0]
        assert request.purpose is CallPurpose.OPTIONAL
        assert len(request.context["source_ids"]) == 8
        assert "历史回放" not in request.prompt
    else:
        assert not requests
    requests.clear()
    with runtime.open_core(paths, clock=ManualClock(NOW), to_console=False) as core:
        heart = core.heartbeat
        for channel in ("mock_group_a", "mock_group_b", "mock_private"):
            heart.attach_channel(channel, lambda _message: None)
        heart.wake_up()
        for sample in samples:
            assert heart.submit(sample)
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert not requests
        assert store.entries(LanguageKind.EXPRESSION) == entries
        assert store.entries(LanguageKind.JARGON) == jargon
        if learning:
            mentioned = set(jargon[0].evidence_ids + jargon[1].evidence_ids)
            evidence = core.timeline.query(TimelineQuery(event_type="message"))
            assert {
                row.metadata["source_message_id"] for row in evidence if row.id in mentioned
            } == {sample.source_message_id for sample in samples}
        assert heart.submit(
            Perception(
                "mock_private",
                "local",
                "gg，来聊跑步。",
                VisibilityScope.USER_ONLY,
                TrustLevel.VERIFIED_USER,
            )
        )
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        plan = next(request for request in requests if request.context["task"] == "plan")
        reply = next(request for request in requests if request.context["task"] == "reply")
        injected = learning and budget > 0
        assert ("GG：抵达终点" in plan.prompt) is injected
        assert ("GG：等候开局" in plan.prompt) is injected
        assert ("当「庆祝跑步成绩」时，可用「月兔冲线」" in reply.prompt) is injected
        assert heart.submit(
            Perception(
                "mock_private",
                "local",
                "陌生主题。",
                VisibilityScope.USER_ONLY,
                TrustLevel.VERIFIED_USER,
            )
        )
        requests.clear()
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert not any("习得表达（" in request.prompt for request in requests)
        await heart.shut_down()
