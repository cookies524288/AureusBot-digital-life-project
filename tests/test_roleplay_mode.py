"""1.12 验收（docs/08-roadmap.md，D65）：扮演模式开关前后，同样的输入走两条路径。

走 open_core 组装路径，器官用假的，不花钱。关着：有真实的身体状态摘要、有样本；
开着：系统上下文里只有一段固定提示词，没有真实数字，也不记样本；安全检查照常。
"""

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.body import BodySampleStore
from aureus.channels import CHANNEL_NAME, TERMINAL_USER_ID
from aureus.common import AureusPaths, ManualClock, shutdown_logging
from aureus.core.runtime import Core, open_core
from aureus.heartbeat import MessageKind, OutgoingMessage, Perception, TrustLevel, VisibilityScope
from aureus.heartbeat.body_summary import ROLEPLAY_NOTE
from aureus.organs import CallAttempt, FakeReplyer, OrganRequest, OrganResponse

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
SAMPLE_INTERVAL = timedelta(seconds=61)
BODY_HEADER = "【身体状态】"

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


def configure(paths: AureusPaths, *, roleplay: bool, limit: int = 100_000) -> None:
    text = (
        f"[heartbeat]\nroleplay_mode = {str(roleplay).lower()}\n"
        f"[safety]\nmonthly_token_limit = {limit}\n"
    )
    paths.runtime_config_file.write_text(text, encoding="utf-8")


async def chat(core: Core, clock: ManualClock, text: str) -> list[OutgoingMessage]:
    sent: list[OutgoingMessage] = []
    core.heartbeat.attach_channel(CHANNEL_NAME, sent.append)
    core.heartbeat.wake_up()
    assert core.heartbeat.submit(
        Perception(
            channel=CHANNEL_NAME,
            sender_id=TERMINAL_USER_ID,
            content=text,
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    await core.heartbeat.beat()
    await core.heartbeat.wait_for_thought()
    await core.heartbeat.beat()
    clock.advance(SAMPLE_INTERVAL)
    await core.heartbeat.beat()
    return sent


def sample_count(paths: AureusPaths) -> int:
    store = BodySampleStore(paths.database_file, ManualClock(START))
    store.open()
    try:
        return len(store.since(START - timedelta(days=1)))
    finally:
        store.close()


async def test_off_uses_the_real_body_state_and_records_samples(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    configure(paths, roleplay=False)
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        await chat(core, clock, "你好")
    context = requests[0].system_context
    assert BODY_HEADER in context
    assert ROLEPLAY_NOTE not in context
    assert sample_count(paths) >= 1


async def test_on_replaces_the_body_state_with_the_fixed_note(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    configure(paths, roleplay=True)
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        await chat(core, clock, "你好")
    context = requests[0].system_context
    assert ROLEPLAY_NOTE in context
    assert BODY_HEADER not in context
    assert "精力 " not in context  # 没有真实的精力读数
    assert sample_count(paths) == 0


async def test_on_builds_no_body_and_no_energy_gate(paths: AureusPaths) -> None:
    configure(paths, roleplay=True)
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        assert core.heartbeat._body is None
        assert core.heartbeat._samples is None


async def test_on_does_not_tell_it_how_long_it_was_stopped(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    configure(paths, roleplay=True)
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        await chat(core, clock, "你好")
    clock.advance(timedelta(hours=3))
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        await chat(core, clock, "在吗")
    assert "停机期间" not in requests[-1].system_context


async def test_the_monthly_limit_still_rejects_in_roleplay_mode(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    configure(paths, roleplay=True, limit=10)
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        sent: list[OutgoingMessage] = []
        core.heartbeat.attach_channel(CHANNEL_NAME, sent.append)
        core.heartbeat.wake_up()
        for text in ("你好", "在吗", "还在吗"):
            assert core.heartbeat.submit(
                Perception(
                    channel=CHANNEL_NAME,
                    sender_id=TERMINAL_USER_ID,
                    content=text,
                    visibility=VisibilityScope.USER_ONLY,
                    trust=TrustLevel.VERIFIED_USER,
                )
            )
            await core.heartbeat.beat()
            await core.heartbeat.wait_for_thought()
            await core.heartbeat.beat()
    # 安全检查不归身体管：每次 6 个 token，第三次超过上限 10，被拒绝。
    assert len(requests) == 2
    assert sent[-1].kind is MessageKind.NOTICE
