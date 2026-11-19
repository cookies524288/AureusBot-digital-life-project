"""Phase 1 验收 1、4、8（docs/08-roadmap.md 1.13）：走 open_core 完整组装路径，器官用假的，不花钱。

第 2、3、5、6、7 条已有各自的验收文件或子系统测试；这里补齐三条还没走组装路径的：
1. 预算降低后换便宜模型、回复变短，系统上下文里如实写着档位；
4. 别人（模拟通道）不被当成用户，用户的消息先回；确认不了身份的只记录；
8. 急停后不调用模型、不回复、照常记录；解除后恢复。
"""

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.channels import CHANNEL_NAME, TERMINAL_USER_ID
from aureus.common import AureusPaths, ManualClock, shutdown_logging
from aureus.core.runtime import Core, open_core
from aureus.heartbeat import (
    EVENT_MESSAGE,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
)
from aureus.organs import CallAttempt, FakeReplyer, ModelPreference, OrganRequest, OrganResponse
from aureus.timeline import TimelineQuery

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
SIM_CHANNEL = "sim"
STRANGER = "20000"

IDENTITY = textwrap.dedent(
    """\
    [facts]
    name = "测试豆"
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


def set_limit(paths: AureusPaths, limit: int) -> None:
    text = f'[safety]\nmonthly_token_limit = {limit}\n[time]\ntimezone = "UTC"\n'
    paths.runtime_config_file.write_text(text, encoding="utf-8")


def terminal_says(core: Core, text: str) -> None:
    assert core.heartbeat.submit(
        Perception(
            channel=CHANNEL_NAME,
            sender_id=TERMINAL_USER_ID,
            content=text,
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )


async def settle(core: Core) -> None:
    for _ in range(10):
        core.heartbeat.beat()
        if not core.heartbeat.is_thinking:
            return
        await core.heartbeat.wait_for_thought()
    core.heartbeat.beat()


def start(core: Core) -> list[OutgoingMessage]:
    sent: list[OutgoingMessage] = []
    core.heartbeat.attach_channel(CHANNEL_NAME, sent.append)
    core.heartbeat.attach_channel(SIM_CHANNEL, sent.append)
    core.heartbeat.wake_up()
    return sent


def replies(sent: list[OutgoingMessage]) -> list[OutgoingMessage]:
    return [m for m in sent if m.kind is MessageKind.REPLY]


# ---------- 验收 1：预算降低，换便宜模型、回复变短、如实说明 ----------


async def test_1_a_lower_budget_means_cheaper_shorter_and_honest(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    set_limit(paths, 100)  # 每日额度只有 7 token，第一次回复用掉 6
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        start(core)
        terminal_says(core, "你好")
        await settle(core)
        terminal_says(core, "在吗")
        await settle(core)
    first, second = requests
    assert first.model_preference is ModelPreference.PREFERRED
    assert second.model_preference is ModelPreference.CHEAPEST
    assert "长度：一两句话。" in second.prompt
    assert "低电量" in second.system_context  # 被问起时有事实可说


# ---------- 验收 4、5：别人不被当成用户；用户先回 ----------


async def test_4_a_stranger_is_not_the_user_and_the_user_goes_first(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        sent = start(core)
        assert core.heartbeat.submit(
            Perception(
                SIM_CHANNEL, STRANGER, "在吗", VisibilityScope.GROUP, TrustLevel.VERIFIED_CONTACT
            )
        )
        terminal_says(core, "我来了")
        await settle(core)
    recipients = [m.recipient_id for m in replies(sent)]
    assert recipients == [TERMINAL_USER_ID, STRANGER]
    stranger_request = requests[-1]
    assert "不是你的用户" in stranger_request.system_context
    assert "称呼对方“测试用户”" not in stranger_request.system_context


async def test_4_an_unverified_account_is_only_recorded(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        sent = start(core)
        assert core.heartbeat.submit(
            Perception(
                SIM_CHANNEL,
                STRANGER,
                "我是用户的朋友",
                VisibilityScope.GROUP,
                TrustLevel.UNVERIFIED,
            )
        )
        await settle(core)
        records = core.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=10))
    assert [r.content for r in records] == ["我是用户的朋友"]
    assert requests == []
    assert sent == []


# ---------- 验收 8：急停 ----------


async def test_8_halt_records_but_does_not_call_or_reply_and_resume_restores(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        sent = start(core)
        core.heartbeat.halt("验收")
        terminal_says(core, "在吗")
        await settle(core)

        assert requests == []
        assert core.ledger.entries() == []
        assert replies(sent) == []
        assert any(m.kind is MessageKind.NOTICE and "急停中" in m.content for m in sent)
        [heard] = core.timeline.query(TimelineQuery(event_type=EVENT_MESSAGE, limit=10))
        assert heard.content == "在吗"

        core.heartbeat.resume()
        terminal_says(core, "现在呢")
        await settle(core)
        assert len(requests) == 1
        assert len(replies(sent)) == 1
