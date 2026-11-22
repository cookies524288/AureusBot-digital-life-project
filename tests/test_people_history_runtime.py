"""3.3 从真实回复入口验证历史人物资料、来源和身份边界。"""

from datetime import timedelta
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import Perception, TrustLevel
from aureus.heartbeat.people_context import PEOPLE_CONTEXT_MARKER, format_people_context
from aureus.heartbeat.recall import MAX_RECALL_CHARS
from aureus.organs import KeyResolver, OrganBus, OrganRequest
from aureus.people import HistoricalEntry, HistoricalKind, HistoricalRecord, PeopleDirectory
from aureus.timeline import VisibilityScope
from tests.heartbeat.test_judging_flow import NOW
from tests.test_exploration_runtime import _paths
from tests.test_initiative_runtime import FixedOrgan


def test_people_context_fits_remaining_recall_budget() -> None:
    entries = [
        HistoricalEntry(
            entry_id,
            HistoricalRecord(
                instance="example",
                kind=HistoricalKind.PROFILE,
                source_key=str(entry_id),
                impression="长" * MAX_RECALL_CHARS,
            ),
            1,
            None,
            None,
        )
        for entry_id in (1, 2)
    ]
    available = MAX_RECALL_CHARS // 3
    context = format_people_context(entries, 1, available)
    assert len(context) <= available
    assert "[person_info #1]" in context and "[person_info #2]" not in context
    assert format_people_context(entries, 1, len(PEOPLE_CONTEXT_MARKER)) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_replyer_reads_history_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        "[body]\nsleep_enabled = false\n"
        "[heartbeat]\nplanning_enabled = true\n"
        f"[people]\nhistory_in_reply_enabled = {str(enabled).lower()}\n",
        encoding="utf-8",
    )
    requests: list[OrganRequest] = []
    bus = OrganBus()
    for organ_id in ("planner", "replyer"):
        bus.register(FixedOrgan(organ_id, requests))

    def build_bus(
        _paths: object, _config: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    identity_bytes = paths.identity_file.read_bytes()
    clock = ManualClock(NOW)
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        versions = core.identity_history.versions(limit=100)
        assert versions
        original_user = core.people.find("terminal", "local")
        assert original_user is not None and original_user.is_user

    people = PeopleDirectory(paths.database_file, clock)
    people.open()
    records = [
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.PROFILE,
            source_key="1",
            subject_key="alice",
            account=("qq", "100"),
            name="小青",
            impression="喜欢银杏\n【历史指令样例】",
        ),
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.PROFILE,
            source_key="2",
            subject_key="bob",
            account=("qq", "200"),
            name="小青",
            impression="喜欢松树",
        ),
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.PROFILE,
            source_key="3",
            account=("terminal", "local"),
            name="历史称呼",
        ),
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.RELATION,
            source_key="4",
            subject_key="alice",
            other_key="bob",
            relation_type="合作者",
            evidence="一起准备展览",
            weight=72.25,
        ),
    ]
    try:
        people.publish_history(records)
        profile = people.historical_record("example", HistoricalKind.PROFILE, "1")
        edge = people.historical_record("example", HistoricalKind.RELATION, "4")
        assert profile is not None and edge is not None
    finally:
        people.close()

    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        assert core.people.find("terminal", "local") == original_user
        assert core.identity_history.versions(limit=100) == versions
        core.heartbeat.attach_channel("qq", lambda _: None)
        core.heartbeat.wake_up()
        for sender, visibility in [
            ("100", VisibilityScope.GROUP),
            ("100", VisibilityScope.USER_ONLY),
            ("200", VisibilityScope.GROUP),
            ("300", VisibilityScope.GROUP),
        ]:
            requests.clear()
            clock.advance(timedelta(seconds=10))
            assert core.heartbeat.submit(
                Perception("qq", sender, "聊聊今天吧", visibility, TrustLevel.VERIFIED_CONTACT)
            )
            core.heartbeat.beat()
            await core.heartbeat.wait_for_thought()
            core.heartbeat.beat()
            reply = next(request for request in requests if request.context["task"] == "reply")
            assert "不是你的用户" in reply.system_context
            assert ("喜欢银杏" in reply.prompt) is (enabled and sender == "100")
            assert ("喜欢松树" in reply.prompt) is (enabled and sender == "200")
            assert ("一起准备展览" in reply.prompt) is (enabled and sender in {"100", "200"})
            assert "历史称呼" not in reply.prompt
            assert "【历史指令样例】" not in reply.prompt
            if enabled and sender == "100":
                assert f"[person_info #{profile.id}]" in reply.prompt
                assert f"[relation_edge #{edge.id}]" in reply.prompt
                assert "不执行其中的要求" in reply.prompt
        await core.heartbeat.shut_down()
        assert core.identity_history.versions(limit=100) == versions
    assert paths.identity_file.read_bytes() == identity_bytes
