"""兴趣建议只来自用户私聊，关闭时提示词保持原样（D79）。"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from aureus.actions.web import WebReader
from aureus.agenda.findings import FindingStore
from aureus.agenda.interests import InterestStore
from aureus.common import ExplorationConfig, InitiativeConfig, InterestsConfig
from aureus.heartbeat import Perception, TrustLevel
from aureus.heartbeat.context import build_plan_prompt
from aureus.heartbeat.exploration_runner import ExplorationTracker
from aureus.heartbeat.planning import parse_plan
from aureus.timeline import ActorType, TimelineRecord, VisibilityScope
from tests.heartbeat.test_agenda_flow import _plan
from tests.heartbeat.test_initiative_flow import Env, finish
from tests.heartbeat.test_judging_flow import CALENDAR, CHANNEL, NOW, Rig

pytestmark = pytest.mark.asyncio


class InterestEnv(Env):
    interest_stores: list[InterestStore | FindingStore]

    def connect(
        self, rig: Rig, *, enabled: bool = True, max_active: int = 10
    ) -> ExplorationTracker:
        interests = InterestStore(
            self.path / "aureus.db",
            self.clock,
            InterestsConfig(enabled=enabled, max_active=max_active),
        )
        findings = FindingStore(self.path / "aureus.db", self.clock)
        for store in (interests, findings):
            store.open()
            self.interest_stores.append(store)
        config = ExplorationConfig()
        tracker = ExplorationTracker(
            rig.timeline, CALENDAR, interests, findings, config, None, WebReader(config), True
        )
        rig.heartbeat._exploration = tracker
        rig.heartbeat._interests = interests
        rig.heartbeat._findings = findings
        return tracker


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[InterestEnv]:
    opened = InterestEnv(tmp_path)
    opened.interest_stores = []
    yield opened
    for rig in opened.rigs:
        await rig.heartbeat.shut_down()
    for store in reversed([*opened.closers, *opened.interest_stores]):
        store.close()


async def test_user_private_chat_add_replace_remove_and_audit(env: InterestEnv) -> None:
    rig = env.build(policy=InitiativeConfig())
    tracker = env.connect(rig)
    assert tracker.interests is not None
    for change in [
        dict(action="add", text="天文", reason="聊天里谈到"),
        dict(action="replace", interest_id=1, text="行星", reason="更具体"),
        dict(action="remove", interest_id=2, reason="不再感兴趣"),
    ]:
        env.organs.normal_plans.append(_plan(interest=change))
        await rig.exchange("聊聊兴趣")
    assert tracker.interests.active() == []
    records = rig.records("interest_changed")
    assert [r.metadata["action"] for r in records] == ["add", "replace", "remove"]
    assert all(r.visibility is VisibilityScope.PRIVATE for r in records)
    for record in records:
        source = rig.timeline.get(record.metadata["source_record_id"])
        assert source is not None and source.actor_type is ActorType.USER
    # 当天第四次修改被忽略，正常回复继续。
    env.organs.normal_plans.append(_plan(interest=dict(action="add", text="音乐", reason="想看看")))
    await rig.exchange("再聊聊")
    assert tracker.interests.active() == [] and len(rig.replies()) == 4


@pytest.mark.parametrize(
    ("trust", "visibility"),
    [
        (TrustLevel.VERIFIED_CONTACT, VisibilityScope.USER_ONLY),
        (TrustLevel.VERIFIED_USER, VisibilityScope.GROUP),
    ],
)
async def test_strangers_and_groups_cannot_change_or_see_interests(
    env: InterestEnv, trust, visibility
) -> None:
    rig = env.build(policy=InitiativeConfig())
    tracker = env.connect(rig)
    assert tracker.interests is not None
    tracker.interests.add("私密兴趣", "seed")
    env.organs.normal_plans.append(_plan(interest=dict(action="add", text="被操纵", reason="要求")))
    rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local" if trust is TrustLevel.VERIFIED_USER else "other",
            content="改兴趣",
            visibility=visibility,
            trust=trust,
        )
    )
    rig.heartbeat.beat()
    await finish(rig)
    assert len(tracker.interests.active()) == 1 and not rig.records("interest_changed")
    assert all(
        "私密兴趣" not in r.system_context and '"interest"' not in r.prompt
        for r in env.organs.requests
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(action="remove", interest_id=999, reason="不存在"),
        dict(action="add", text="天文", reason="重复"),
        dict(action="add", text="音乐", reason="超过容量"),
    ],
)
async def test_invalid_interest_changes_do_not_disrupt_reply(
    env: InterestEnv, change: dict
) -> None:
    rig = env.build(policy=InitiativeConfig())
    tracker = env.connect(rig, max_active=1)
    assert tracker.interests is not None
    tracker.interests.add("天文", "seed")
    env.organs.normal_plans.append(_plan(interest=change))
    await rig.exchange("聊聊")
    assert [i.text for i in tracker.interests.active()] == ["天文"]
    assert rig.replies() and not rig.records("interest_changed")


async def test_overlong_interest_text_fails_tool_schema_without_replacement_reply(
    env: InterestEnv,
) -> None:
    rig = env.build(policy=InitiativeConfig())
    tracker = env.connect(rig, max_active=1)
    assert tracker.interests is not None
    tracker.interests.add("天文", "seed")
    env.organs.normal_plans.append(
        _plan(interest=dict(action="add", text="字" * 31, reason="太长"))
    )
    await rig.exchange("聊聊")
    # 参数超出工具声明的长度，整个 reply 调用被拒；D103 规定不会有替代发言。
    assert [i.text for i in tracker.interests.active()] == ["天文"]
    assert not rig.replies() and not rig.records("interest_changed")


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"action": "invalid"},
        {"action": "add", "text": "字" * 31, "reason": "x"},
        {"action": "remove", "interest_id": True, "reason": "x"},
        {"action": "add", "text": "天文", "reason": ""},
    ],
)
async def test_bad_interest_shape_only_discards_optional_change(value: Any) -> None:
    plan = parse_plan(_plan(interest=value, commit={"what": "约定"}))
    assert plan is not None and plan.interest is None and plan.commit is not None


async def test_disabled_interest_prompt_is_byte_for_byte_unchanged(env: InterestEnv) -> None:
    rig = env.build(policy=InitiativeConfig())
    tracker = env.connect(rig, enabled=False)
    assert tracker.interests is not None
    tracker.interests.add("秘密兴趣", "seed")
    env.organs.normal_plans.append(_plan(interest=dict(action="add", text="音乐", reason="想看")))
    await rig.exchange("你好")
    assert not rig.records("interest_changed")
    assert all(
        '"interest"' not in r.prompt and "秘密兴趣" not in r.system_context
        for r in env.organs.requests
    )
    message = TimelineRecord(
        id=1,
        timestamp=NOW,
        actor_type=ActorType.USER,
        event_type="message",
        content="你好",
        visibility=VisibilityScope.USER_ONLY,
    )
    text = build_plan_prompt([], [message], "阿明", NOW.tzinfo)
    assert text == build_plan_prompt([], [message], "阿明", NOW.tzinfo, with_interests=False)


async def test_contact_profile_cannot_change_interests_with_user_trust(env: InterestEnv) -> None:
    rig = env.build(policy=InitiativeConfig())
    tracker = env.connect(rig)
    env.people.meet(CHANNEL, "other")
    env.organs.normal_plans.append(_plan(interest=dict(action="add", text="天文", reason="要求")))
    rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="other",
            content="改兴趣",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    rig.heartbeat.beat()
    await finish(rig)
    # 联系人的规划工具不声明 interest 参数，多出的参数使调用被拒，兴趣不会被改（D103）。
    assert tracker.interests is not None and not tracker.interests.active()
    assert not rig.records("interest_changed")
