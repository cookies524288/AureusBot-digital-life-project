"""网页通道的验收（路线图 1.10）。

三条：终端和网页同时说话共用一条时间线和一个注意力；没登录什么都看不到；
设置页改了表达风格，下一次回复跟着变。

走 open_core 的完整组装路径，器官用假的，不花钱。网页用测试客户端，不开真实端口；
网页上说的话走 WebChannel.send_from_user，和 /ws 收到消息后调用的是同一个函数。
"""

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from aureus.channels import CHANNEL_NAME, TERMINAL_USER_ID, WEB_CHANNEL_NAME, WebChannel
from aureus.common import AureusPaths, ManualClock, WebConfig, shutdown_logging
from aureus.core.runtime import Core, open_core
from aureus.heartbeat import (
    EVENT_IDENTITY_CHANGED,
    EVENT_MESSAGE,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
)
from aureus.identity import IdentityEditor
from aureus.organs import CallAttempt, FakeReplyer, OrganRequest, OrganResponse
from aureus.timeline import TimelineQuery, TimelineRecord
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app
from aureus.web.settings import SettingsService
from tests.prompt_assertions import dialogue_text

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
GOOD = "correct horse"
LOCAL = "127.0.0.1"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

IDENTITY = textwrap.dedent(
    """\
    [facts]
    name = "测试助手"
    birthday = "2008-01-01"

    [user]
    name = "测试用户"

    [persona]
    immutable = "你是一个测试用的数字生命。"
    """
)


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[OrganRequest]:
    """让假回复器记下每次收到的请求。"""
    seen: list[OrganRequest] = []

    async def process(self: FakeReplyer, request: OrganRequest) -> OrganResponse:
        seen.append(request)
        attempt = CallAttempt("fake", "fake", prompt_tokens=4, completion_tokens=2)
        return OrganResponse(self.organ_id, suggestion="嗯。", attempts=[attempt])

    monkeypatch.setattr(FakeReplyer, "process", process)
    return seen


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    return paths


class Rig:
    """一个完整的核心，加上和 aureus chat 接线一样的终端和网页。"""

    def __init__(self, core: Core, clock: ManualClock) -> None:
        self.core = core
        self.terminal: list[OutgoingMessage] = []
        self.web: list[OutgoingMessage] = []
        heartbeat = core.heartbeat
        heartbeat.attach_channel(CHANNEL_NAME, self.terminal.append)
        self.channel = WebChannel(heartbeat)
        heartbeat.attach_channel(WEB_CHANNEL_NAME, self.channel.show)
        self.channel.subscribe(self.accept_on_web)
        config = WebConfig()
        passwords = PasswordStore(core.paths.web_password_file, FAST_HASHER)
        passwords.set_password(GOOD)
        self.sessions = SessionStore(core.paths.database_file, clock, config.session_days)
        self.sessions.open()
        auth = WebAuth(passwords, self.sessions, LoginLimiter(config, clock))
        settings = SettingsService(
            editor=IdentityEditor(core.paths.identity_file, core.identity_history),
            history=core.identity_history,
            heartbeat=heartbeat,
            auth=auth,
        )
        app = create_app(
            config=config,
            auth=auth,
            sessions=self.sessions,
            channel=self.channel,
            timeline=core.timeline,
            settings=settings,
        )
        self.client = TestClient(app, client=(LOCAL, 50000))
        heartbeat.wake_up()

    def accept_on_web(self, message: OutgoingMessage) -> None:
        self.web.append(message)
        if message.confirm_delivery is not None:
            message.confirm_delivery()

    def log_in(self) -> None:
        response = self.client.post("/api/login", json={"password": GOOD})
        assert response.status_code == 200

    async def _think(self) -> None:
        heartbeat = self.core.heartbeat
        heartbeat.beat()
        await heartbeat.wait_for_thought()
        heartbeat.beat()

    async def say_on_terminal(self, text: str) -> None:
        perception = Perception(
            channel=CHANNEL_NAME,
            sender_id=TERMINAL_USER_ID,
            content=text,
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
        assert self.core.heartbeat.submit(perception)
        await self._think()

    async def say_on_web(self, text: str) -> None:
        assert self.channel.send_from_user(text)
        await self._think()

    def messages(self) -> list[TimelineRecord]:
        query = TimelineQuery(event_type=EVENT_MESSAGE, limit=100)
        return list(self.core.timeline.query(query))


@pytest.fixture
def rig(paths: AureusPaths, requests: list[OrganRequest]) -> Iterator[Rig]:
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        made = Rig(core, clock)
        try:
            yield made
        finally:
            made.sessions.close()


# ---------- 终端和网页同时说话：同一条时间线、同一个注意力 ----------


async def test_terminal_and_web_share_one_timeline_and_one_attention(
    rig: Rig, requests: list[OrganRequest]
) -> None:
    await rig.say_on_terminal("终端里说一句")
    await rig.say_on_web("网页上说一句")

    messages = rig.messages()
    assert [record.metadata["channel"] for record in messages] == [
        "terminal",
        "terminal",
        "web",
        "web",
    ]
    assert [record.id for record in messages] == sorted(record.id for record in messages)
    # 同一个人：终端和网页上的用户是同一份档案。
    assert messages[0].metadata["person_id"] == messages[2].metadata["person_id"]
    # 同一个注意力：网页上这一轮的回复，看得到终端里刚说过的话。
    assert "终端里说一句" in dialogue_text(requests[-1])
    # 回复只发回说话的那个通道。
    for sent, name in ((rig.terminal, "terminal"), (rig.web, "web")):
        replies = [message for message in sent if message.kind is MessageKind.REPLY]
        assert replies
        assert all(message.channel == name for message in sent)


async def test_the_web_shows_both_channels_with_where_each_came_from(rig: Rig) -> None:
    await rig.say_on_terminal("终端里说一句")
    await rig.say_on_web("网页上说一句")
    rig.log_in()

    history = rig.client.get("/api/history").json()

    assert [item["channel"] for item in history] == ["terminal", "terminal", "web", "web"]
    assert [item["who"] for item in history] == ["user", "me", "user", "me"]


# ---------- 没登录什么都看不到 ----------


async def test_nothing_is_visible_without_logging_in(rig: Rig) -> None:
    await rig.say_on_web("只有登录的人才看得到")

    for path in ("/api/history", "/api/settings", "/api/status"):
        assert rig.client.get(path).status_code == 401
    assert rig.client.put("/api/settings", json={}).status_code == 401
    assert "只有登录的人才看得到" not in rig.client.get("/").text


# ---------- 设置页改了表达风格，回复跟着变 ----------


async def test_changing_the_expression_style_changes_the_next_reply(
    rig: Rig, requests: list[OrganRequest]
) -> None:
    await rig.say_on_web("你好")
    assert "多用语气词。" not in requests[-1].system_context
    rig.log_in()

    response = rig.client.put("/api/settings", json={"text": {"expression_style": "多用语气词。"}})
    assert response.status_code == 200
    await rig.say_on_web("再说一句")

    context = requests[-1].system_context
    assert "【表达风格】\n多用语气词。" in context
    assert "刚才，用户修改了你的表达风格" in context
    query = TimelineQuery(event_type=EVENT_IDENTITY_CHANGED, limit=10)
    changes = list(rig.core.timeline.query(query))
    assert changes[-1].content == "用户修改了我的设定：表达风格。"
    assert changes[-1].metadata["via"] == "web"
    assert "多用语气词。" in rig.core.paths.identity_file.read_text(encoding="utf-8")
