"""注意力和人际的流程测试（路线图 1.8，D61）。

验收：同时有用户和别人的消息时先回用户；别人能被回复，系统上下文里有防套话的说明；
确认不了身份的账号仍然只记录、不回复；人物档案按账号建。
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import (
    EVENT_MESSAGE,
    Heartbeat,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage
from aureus.identity import Identity
from aureus.organs import OrganRequest, OrganResponse
from aureus.people import HistoricalKind, HistoricalRecord, PeopleDirectory
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope
from aureus.web.resource_api import register_resource_routes
from tests.prompt_assertions import dialogue_text

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
        from tests.planner_fixtures import plan_response

        if request.context.get("task") == "plan":
            return plan_response(request, {"reply": True, "intent": "简单应答"})
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
        config=HeartbeatConfig(planning_enabled=False, split_reply_enabled=False),
        permissions=PermissionChecker(emergency_stop),
        emergency_stop=emergency_stop,
        timezone=UTC,
        people=people if with_people else None,
    )
    rig = Rig(timeline, people, organs, heartbeat)
    made.append(rig)
    return rig


async def test_an_incoming_display_name_names_a_new_contact(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    perception = Perception(
        CHANNEL, STRANGER, "你好", VisibilityScope.GROUP, TrustLevel.UNVERIFIED
    )
    message = InboxMessage(
        id=1,
        conversation=ConversationKey(CHANNEL, "bot", "private", STRANGER),
        perception=perception,
        sender_name="群友小甲",
        conversation_name="",
        timestamp=NOW,
        status="unread",
    )
    rig.heartbeat._pending.append((perception, message))

    rig.heartbeat._take_in_perceptions()

    person = rig.people.find(CHANNEL, STRANGER)
    assert person is not None and person.name == "群友小甲"


async def test_local_profile_aliases_reach_only_the_exact_accounts_reply(
    made: list[Rig], tmp_path: Path
) -> None:
    rig = make_rig(made, tmp_path)
    rig.people.publish_history(
        [
            HistoricalRecord(
                instance="sample",
                kind=HistoricalKind.PROFILE,
                source_key=account,
                subject_key=account,
                account=(CHANNEL, account),
                nickname="同名",
            )
            for account in ("account-a", "account-b")
        ]
        + [
            HistoricalRecord(
                instance="sample",
                kind=HistoricalKind.PROFILE_SNAPSHOT,
                source_key=f"{account}:{version}",
                subject_key=account,
                profile_version=version,
                aliases=[alias],
                occurred_at=NOW.timestamp(),
                evidence=[version],
            )
            for account, version, alias in (
                ("account-a", 1, "旧版画像别名"),
                ("account-a", 2, "甲原有别名"),
                ("account-b", 1, "乙原有别名"),
                ("unmapped", 1, "待关联别名"),
            )
        ]
    )
    app = FastAPI()

    async def require_session(request: Request) -> str:
        if request.cookies.get("session") != "test":
            raise HTTPException(401)
        return "test"

    register_resource_routes(
        app,
        require_session,
        None,
        None,
        people=rig.people,
        same_origin=lambda request: request.headers.get("origin") == "http://testserver",
    )
    with TestClient(app) as web:
        web.cookies.set("session", "test")
        profiles = {
            item["record"]["source_key"]: item
            for item in web.get("/api/resources/profiles").json()["items"]
            if item["entry_type"] == "profile"
        }
        base_url = "/api/resources/profiles"
        primary = f"{base_url}/{profiles['account-a:2']['id']}/aliases"
        prior = f"{base_url}/{profiles['account-a:1']['id']}/aliases"
        pending = f"{base_url}/{profiles['unmapped:1']['id']}/aliases"
        origin = {"Origin": "http://testserver"}
        web.cookies.clear()
        assert web.get(primary).status_code == 401
        assert web.patch(primary, json={"aliases": ["未登录"]}, headers=origin).status_code == 401
        web.cookies.set("session", "test")
        response = web.get(primary)
        assert response.status_code == 200, response.text
        assert response.json()["effective_aliases"] == ["甲原有别名"]
        assert profiles["account-a:2"]["can_edit_aliases"]
        assert not profiles["unmapped:1"]["can_edit_aliases"]
        assert web.patch(primary, json={"aliases": ["越权"]}).status_code == 403
        assert web.get(pending).status_code == 404
        assert web.patch(pending, json={"aliases": ["不可关联"]}, headers=origin).status_code == 404
        assert web.patch(primary, json={"aliases": [" "]}, headers=origin).status_code == 422
        assert web.get(primary).json()["local_aliases"] is None
        assert web.patch(
            prior, json={"aliases": ["甲新增别名", " Atlas ", "atlas"]}, headers=origin
        ).is_success

        rig.people.close()
        rig.people.open()
        assert web.get(primary).json()["local_aliases"] == ["甲新增别名", "Atlas"]
        person = rig.people.find(CHANNEL, "account-a")
        assert person is not None
        original_entries = rig.people.history_for(person.id, limit=5, use_local_aliases=False)
        assert [entry.record.aliases for entry in original_entries if entry.record.aliases] == [
            ["甲原有别名"]
        ]
        for account, present, absent in (
            ("account-a", "甲新增别名", "乙原有别名"),
            ("account-b", "乙原有别名", "甲新增别名"),
        ):
            rig.say(account, "在吗？", TrustLevel.VERIFIED_CONTACT)
            await rig.settle()
            prompt = dialogue_text(rig.organs.requests[-1])
            assert present in prompt and absent not in prompt
            assert "甲原有别名" not in prompt and "待关联别名" not in prompt

        assert web.patch(primary, json={"aliases": None}, headers=origin).is_success
        rig.say("account-a", "还在吗？", TrustLevel.VERIFIED_CONTACT)
        await rig.settle()
        prompt = dialogue_text(rig.organs.requests[-1])
        assert "甲原有别名" in prompt and "甲新增别名" not in prompt
        original = rig.people.historical_record(
            "sample", HistoricalKind.PROFILE_SNAPSHOT, "account-a:2"
        )
        assert (
            original
            and original.record.aliases == ["甲原有别名"]
            and original.record.evidence == [2]
        )


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
    assert USER_SECRET not in dialogue_text(request) + request.system_context


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

    assert [request.organ_id for request in rig.organs.requests] == ["planner", "replyer"] * 2
    stranger_prompt = dialogue_text(rig.organs.requests[-1])
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
