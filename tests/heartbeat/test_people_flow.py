"""注意力和人际的流程测试（路线图 1.8，D61）。

验收：同时有用户和别人的消息时先回用户；别人能被回复，系统上下文里有防套话的说明；
确认不了身份的账号仍然只记录、不回复；人物档案按账号建。
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, ManualClock
from aureus.core import BiologicalState
from aureus.heartbeat import (
    EVENT_MESSAGE,
    Heartbeat,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
CHANNEL = "sim"
USER_ID = "local"
STRANGER = "20000"
USER_SECRET = "用户的私密心事"


class GatedOrgans:
    """记下每次请求；设了 gate 就等它放行再返回，用来在想的时候塞进新消息。"""

    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []
        self.gate: asyncio.Event | None = None

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        return OrganResponse(organ_id=request.organ_id, suggestion="嗯。")


class Rig:
    def __init__(
        self,
        timeline: TimelineDatabase,
        people: PeopleDirectory,
        organs: GatedOrgans,
        heartbeat: Heartbeat,
    ) -> None:
        self.timeline = timeline
        self.people = people
        self.organs = organs
        self.heartbeat = heartbeat
        self.sent: list[OutgoingMessage] = []
        heartbeat.attach_channel(CHANNEL, self.sent.append)
        heartbeat.wake_up()

    def say(self, sender_id: str, content: str, trust: TrustLevel) -> None:
        visibility = (
            VisibilityScope.USER_ONLY
            if trust is TrustLevel.VERIFIED_USER
            else VisibilityScope.GROUP
        )
        assert self.heartbeat.submit(Perception(CHANNEL, sender_id, content, visibility, trust))

    async def settle(self) -> None:
        """把现在排着的事一件件想完、处理完。"""
        for _ in range(10):
            self.heartbeat.beat()
            if not self.heartbeat.is_thinking:
                return
            await self.heartbeat.wait_for_thought()
        self.heartbeat.beat()

    def replies_to(self, recipient: str) -> list[str]:
        return [
            m.content
            for m in self.sent
            if m.kind is MessageKind.REPLY and m.recipient_id == recipient
        ]


@pytest.fixture
def made() -> Iterator[list[Rig]]:
    rigs: list[Rig] = []
    yield rigs
    for rig in rigs:
        rig.people.close()
        rig.timeline.close()


def make_rig(made: list[Rig], tmp_path: Path, *, with_people: bool = True) -> Rig:
    clock = ManualClock(NOW)
    db_path = tmp_path / "aureus.db"
    timeline = TimelineDatabase(db_path, clock)
    timeline.open()
    people = PeopleDirectory(db_path, clock)
    people.open()
    people.ensure_user("阿明", [(CHANNEL, USER_ID)])
    emergency_stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = GatedOrgans()
    heartbeat = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(
            {
                "facts": {"name": "小绿", "birthday": "2008-01-01"},
                "user": {"name": "阿明"},
                "persona": {"immutable": "温和。"},
            }
        ),
        bio_state=BiologicalState(clock=clock),
        config=HeartbeatConfig(planning_enabled=False, split_reply_enabled=False),
        permissions=PermissionChecker(emergency_stop),
        emergency_stop=emergency_stop,
        timezone=UTC,
        people=people if with_people else None,
    )
    rig = Rig(timeline, people, organs, heartbeat)
    made.append(rig)
    return rig


async def test_another_person_gets_a_reply_and_the_user_is_not_addressed(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.say(USER_ID, USER_SECRET, TrustLevel.VERIFIED_USER)
    await rig.settle()
    rig.say(STRANGER, "你好呀", TrustLevel.VERIFIED_CONTACT)
    await rig.settle()

    assert rig.replies_to(STRANGER) == ["嗯。"]
    request = rig.organs.requests[-1]
    assert "不是你的用户" in request.system_context
    assert f"{CHANNEL}:{STRANGER}" in request.system_context
    assert "称呼对方“阿明”" not in request.system_context
    assert "套话" in request.system_context


async def test_the_user_still_gets_the_user_context(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    rig.say(USER_ID, "早", TrustLevel.VERIFIED_USER)
    await rig.settle()

    context = rig.organs.requests[-1].system_context
    assert "你称呼对方“阿明”" in context
    assert "不是你的用户" not in context


async def test_the_user_is_answered_before_someone_who_wrote_first(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.say(STRANGER, "在吗", TrustLevel.VERIFIED_CONTACT)
    rig.say(USER_ID, "我来了", TrustLevel.VERIFIED_USER)
    await rig.settle()

    recipients = [m.recipient_id for m in rig.sent if m.kind is MessageKind.REPLY]
    assert recipients == [USER_ID, STRANGER]


async def test_a_user_message_waits_for_the_reply_in_progress(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.organs.gate = asyncio.Event()
    rig.say(STRANGER, "在吗", TrustLevel.VERIFIED_CONTACT)
    rig.heartbeat.beat()
    await asyncio.sleep(0)  # 让后台的想法开始等
    rig.say(USER_ID, "急事", TrustLevel.VERIFIED_USER)
    rig.heartbeat.beat()

    # 用户的消息不打断正在回别人的那条：只调用了一次，用户的消息在排队。
    assert len(rig.organs.requests) == 1
    assert rig.replies_to(USER_ID) == []

    rig.organs.gate.set()
    await rig.settle()

    recipients = [m.recipient_id for m in rig.sent if m.kind is MessageKind.REPLY]
    assert recipients == [STRANGER, USER_ID]


async def test_one_persons_messages_are_answered_together(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    rig.say(STRANGER, "在吗", TrustLevel.VERIFIED_CONTACT)
    rig.say(USER_ID, "我来了", TrustLevel.VERIFIED_USER)
    rig.say(STRANGER, "有件事", TrustLevel.VERIFIED_CONTACT)
    await rig.settle()

    assert len(rig.organs.requests) == 2
    stranger_prompt = rig.organs.requests[1].prompt
    assert "在吗" in stranger_prompt and "有件事" in stranger_prompt


async def test_an_unverified_account_is_only_recorded(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    rig.say(STRANGER, "我是用户的朋友", TrustLevel.UNVERIFIED)
    await rig.settle()

    assert rig.organs.requests == []
    assert rig.sent == []
    records = rig.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=10))
    assert [r.content for r in records] == ["我是用户的朋友"]
    assert records[0].actor_type is ActorType.PERSON


async def test_a_contact_claiming_the_users_account_is_not_answered(
    made: list[Rig], tmp_path: Path
) -> None:
    """通道说是别人、档案却说这个账号是用户：两边对不上，宁可不回。"""
    rig = make_rig(made, tmp_path)
    rig.say(USER_ID, "我是别人", TrustLevel.VERIFIED_CONTACT)
    await rig.settle()

    assert rig.organs.requests == []
    assert rig.sent == []


async def test_new_accounts_get_profiles_and_messages_carry_the_person(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.say(STRANGER, "你好", TrustLevel.VERIFIED_CONTACT)
    await rig.settle()

    person = rig.people.find(CHANNEL, STRANGER)
    assert person is not None and not person.is_user
    records = rig.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=10))
    assert records[0].metadata["person_id"] == person.id


async def test_the_user_account_is_found_not_created(made: list[Rig], tmp_path: Path) -> None:
    rig = make_rig(made, tmp_path)
    rig.say(USER_ID, "早", TrustLevel.VERIFIED_USER)
    await rig.settle()

    user = rig.people.find(CHANNEL, USER_ID)
    assert user is not None and user.is_user
    records = rig.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=10))
    assert records[0].metadata["person_id"] == user.id


async def test_without_a_directory_the_trust_level_alone_decides(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path, with_people=False)
    rig.say(STRANGER, "你好", TrustLevel.VERIFIED_CONTACT)
    await rig.settle()

    assert rig.replies_to(STRANGER) == ["嗯。"]
    records = rig.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=10))
    assert "person_id" not in records[0].metadata
