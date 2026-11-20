"""有效对话的更新和重启恢复：必须确实向用户说出口（D76）。"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from aureus.heartbeat import (
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.organs import OrganRequest, OrganResponse
from aureus.timeline import ActorType, VisibilityScope
from tests.heartbeat.test_agenda_flow import Env
from tests.heartbeat.test_judging_flow import CHANNEL, NOW, PLAN

pytestmark = pytest.mark.asyncio
DECLINE = '{"reply":false,"reason":"不用回"}'


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    opened = Env(tmp_path)
    yield opened
    opened.close()


async def test_only_an_actual_user_reply_updates_conversation_time(env: Env) -> None:
    rig, _, _ = env.build([PLAN, DECLINE], agenda_enabled=False)
    assert rig.heartbeat._last_conversation_at is None
    await rig.exchange("你好")
    assert rig.heartbeat._last_conversation_at == NOW
    assert rig.heartbeat._reading is not None
    assert rig.heartbeat._reading.social_need.level == 0
    assert rig.records("message")[-1].metadata["to_user"] is True
    env.clock.advance(timedelta(hours=2))
    await rig.exchange("嗯")
    assert rig.heartbeat._last_conversation_at == NOW
    assert rig.heartbeat._reading.social_need.hours_since == 2


@pytest.mark.parametrize("failure", ["halt", "empty", "channel", "disconnected", "organ"])
async def test_unsent_reply_is_not_an_effective_conversation(
    env: Env, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _, _ = env.build([PLAN], agenda_enabled=False)
    if failure == "halt":
        rig.heartbeat.halt("测试")
        await rig.exchange("你好")
    else:
        rig.heartbeat.submit(
            Perception(
                channel=CHANNEL,
                sender_id="local",
                content="你好",
                visibility=VisibilityScope.USER_ONLY,
                trust=TrustLevel.VERIFIED_USER,
            )
        )
        if failure in ("empty", "organ"):
            original = env.organs.call

            async def reply(request: OrganRequest) -> OrganResponse:
                if request.organ_id == "replyer":
                    if failure == "organ":
                        raise OSError("测试调用失败")
                    return OrganResponse(organ_id=request.organ_id, suggestion="   ")
                return await original(request)

            monkeypatch.setattr(env.organs, "call", reply)
        rig.heartbeat.beat()
        await rig.heartbeat.wait_for_thought()
        if failure == "channel":

            def broken(message: OutgoingMessage) -> None:
                raise OSError("通道坏了")

            rig.heartbeat._sinks[CHANNEL] = broken
        elif failure == "disconnected":
            rig.heartbeat._sinks.clear()
        rig.heartbeat.beat()
    assert rig.heartbeat._last_conversation_at is None
    assert not any(message.kind is MessageKind.REPLY for message in rig.sent)


async def test_contact_reply_does_not_count_as_user_conversation(env: Env) -> None:
    rig, _, _ = env.build([PLAN], agenda_enabled=False)
    rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="other",
            content="你好",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_CONTACT,
        )
    )
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    rig.heartbeat.beat()
    assert rig.replies() and rig.heartbeat._last_conversation_at is None


async def test_restart_restores_last_effective_conversation(env: Env) -> None:
    rig, _, _ = env.build([PLAN], agenda_enabled=False)
    await rig.exchange("你好")
    await rig.heartbeat.shut_down()
    env.clock.advance(timedelta(hours=12))
    restored, _, _ = env.build([], agenda_enabled=False)
    assert restored.heartbeat._last_conversation_at == NOW
    restored.heartbeat.beat()
    assert restored.heartbeat._reading is not None
    assert restored.heartbeat._reading.social_need.hours_since == 12


async def test_conversation_time_is_delivery_time_not_arrival_time(env: Env) -> None:
    rig, _, _ = env.build([PLAN], agenda_enabled=False)
    rig.heartbeat.submit(
        Perception(
            channel=CHANNEL,
            sender_id="local",
            content="你好",
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    env.clock.advance(timedelta(hours=1))
    rig.heartbeat.beat()
    assert rig.heartbeat._last_conversation_at == env.clock.now()
    assert rig.records("message")[-1].timestamp == env.clock.now()


async def test_legacy_user_reply_is_restored_but_initiative_is_not(env: Env) -> None:
    rig, _, _ = env.build([], agenda_enabled=False)
    source = rig.timeline.append(
        event_type="message",
        content="用户说了",
        actor_type=ActorType.USER,
        visibility=VisibilityScope.USER_ONLY,
    )
    rig.timeline.append(
        event_type="message",
        content="老版本的回复",
        actor_type=ActorType.ME,
        visibility=VisibilityScope.USER_ONLY,
        metadata={"in_reply_to": [source.id]},
    )
    env.clock.advance(timedelta(hours=1))
    rig.timeline.append(
        event_type="message",
        content="主动开口",
        actor_type=ActorType.ME,
        visibility=VisibilityScope.USER_ONLY,
        metadata={"to_user": True, "initiative": True},
    )
    restored, _, _ = env.build([], agenda_enabled=False)
    assert restored.heartbeat._last_conversation_at == NOW
