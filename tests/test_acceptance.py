"""Phase 0 验收（docs/08-roadmap.md 0.11）：用 open_core 组装出完整的核心，逐条验证。

和各子系统自己的测试不同，这里走的是 `aureus chat` 实际用的那条组装路径，
器官用假的，不花钱。验收第 2、6 条分别由 tests/core/test_runtime.py 和
scripts/check.py 负责，第 7 条的搬家、写入范围、写死盘符三项由 tests/test_portability.py 负责。
"""

import shutil
import sqlite3
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.channels import CHANNEL_NAME, TERMINAL_USER_ID
from aureus.common import AcceleratedClock, AureusPaths, ManualClock, shutdown_logging
from aureus.core.portability import export_data, import_data
from aureus.core.runtime import Core, open_core
from aureus.heartbeat import (
    EVENT_SHUT_DOWN,
    MessageKind,
    OutgoingMessage,
    Perception,
    TrustLevel,
    VisibilityScope,
)
from aureus.organs import CallAttempt, FakeReplyer, OrganRequest, OrganResponse
from aureus.timeline import ActorType, TimelineQuery

START = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
# 选一个离现在很远的年份：时间线上出现真实时间的话，一眼就能看出来。
FAR_FUTURE = datetime(2040, 1, 1, tzinfo=UTC)
TOKENS_PER_REPLY = 6

pytestmark = pytest.mark.asyncio

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
    """让假回复器记下每次收到的请求，并报告 6 个 token，这样账本和预算才有东西可查。"""
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


def attach_terminal(core: Core) -> list[OutgoingMessage]:
    sent: list[OutgoingMessage] = []
    core.heartbeat.attach_channel(CHANNEL_NAME, sent.append)
    return sent


async def converse(core: Core, text: str) -> None:
    """用户说一句：跳一拍开始想，等想完，再跳一拍把回复说出来。"""
    accepted = core.heartbeat.submit(
        Perception(
            channel=CHANNEL_NAME,
            sender_id=TERMINAL_USER_ID,
            content=text,
            visibility=VisibilityScope.USER_ONLY,
            trust=TrustLevel.VERIFIED_USER,
        )
    )
    assert accepted
    core.heartbeat.beat()
    await core.heartbeat.wait_for_thought()
    core.heartbeat.beat()


def all_records(core: Core) -> list:
    return list(core.timeline.query(TimelineQuery(limit=1000)))


# ---------- 验收 1：聊几句，关掉，重启，记得聊了什么、停了多久 ----------


async def test_1_restart_remembers_the_chat_and_the_downtime(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        attach_terminal(core)
        core.heartbeat.wake_up()
        await converse(core, "我叫阿明，今天面试被拒了")
        await core.heartbeat.shut_down()

    clock.advance(timedelta(hours=3, minutes=20))
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        attach_terminal(core)
        downtime = core.heartbeat.wake_up()
        await converse(core, "还记得我刚才说了什么吗")

    assert downtime.shutdown_recorded
    assert downtime.duration == timedelta(hours=3, minutes=20)
    second_run_request = requests[-1]
    assert "停了 3 小时 20 分钟" in second_run_request.system_context
    assert "今天面试被拒了" in second_run_request.prompt


# ---------- 验收 3：每次调用的 token 查得到；预算调低后超限就拒绝 ----------


async def test_3_every_call_is_in_the_ledger_and_a_low_budget_rejects(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    paths.runtime_config_file.write_text("[safety]\nmonthly_token_limit = 10\n", encoding="utf-8")
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        sent = attach_terminal(core)
        core.heartbeat.wake_up()
        for text in ("你好", "在吗", "还在吗"):
            await converse(core, text)

        # 第 1、2 次各用 6 个，累计 12，超过上限 10，第 3 次被拒绝，没有调用器官。
        assert [message.kind for message in sent] == [
            MessageKind.REPLY,
            MessageKind.REPLY,
            MessageKind.NOTICE,
        ]
        assert len(requests) == 2
        entries = core.ledger.entries()
        assert [entry.budget_tokens for entry in entries] == [TOKENS_PER_REPLY] * 2
        assert core.ledger.tokens_used_in_month(START) == 2 * TOKENS_PER_REPLY


# ---------- 验收 4：时间线每条记录都有序号、来源和可见范围 ----------


async def test_4_every_timeline_record_has_sequence_source_and_visibility(
    paths: AureusPaths, requests: list[OrganRequest]
) -> None:
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        attach_terminal(core)
        core.heartbeat.wake_up()
        await converse(core, "你好")
        await core.heartbeat.shut_down()
        records = all_records(core)

    assert len(records) >= 4  # 醒来、用户的话、它的回复、关闭，至少这几条
    assert [record.id for record in records] == list(range(1, len(records) + 1))
    for record in records:
        assert isinstance(record.actor_type, ActorType)
        assert isinstance(record.visibility, VisibilityScope)
    assert records[-1].event_type == EVENT_SHUT_DOWN

    # 不只靠代码里的检查：数据库表本身就不允许来源和可见范围为空。
    with sqlite3.connect(paths.database_file) as connection:
        columns = {row[1]: row[3] for row in connection.execute("PRAGMA table_info(timeline)")}
    connection.close()
    assert columns["actor_type"] == 1
    assert columns["visibility"] == 1


# ---------- 验收 5：加速时钟加假器官，时间线记的是加速后的时间 ----------


async def test_5_accelerated_clock_with_fake_organs_costs_nothing(
    paths: AureusPaths,
) -> None:
    real = ManualClock(START)
    accelerated = AcceleratedClock(start=FAR_FUTURE, speed=3600, real_clock=real)
    with open_core(paths, fake_organs=True, clock=accelerated, to_console=False) as core:
        attach_terminal(core)
        core.heartbeat.wake_up()
        real.advance(timedelta(seconds=2))  # 真实 2 秒 = 加速后 2 小时
        await converse(core, "过了两个小时")
        records = all_records(core)
        assert core.ledger.entries() == []  # 假器官不调用模型，一分钱没花

    message = next(record for record in records if record.actor_type is ActorType.USER)
    assert message.timestamp - FAR_FUTURE >= timedelta(hours=2)
    assert all(
        FAR_FUTURE <= record.timestamp < FAR_FUTURE + timedelta(hours=3) for record in records
    )


# ---------- 验收 7：导出的文件在新位置导入后，能继续运行；整个目录搬走也能继续 ----------


async def test_7_exported_data_continues_to_run_in_a_new_place(
    paths: AureusPaths, requests: list[OrganRequest], tmp_path: Path
) -> None:
    clock = ManualClock(START)
    with open_core(paths, fake_organs=True, clock=clock, to_console=False) as core:
        attach_terminal(core)
        core.heartbeat.wake_up()
        await converse(core, "搬家前说的话")
        await core.heartbeat.shut_down()

    archive = tmp_path / "backup.zip"
    export_data(paths, archive, clock)
    imported = AureusPaths(tmp_path / "other_disk" / "imported_home")
    import_data(imported, archive, clock)

    clock.advance(timedelta(hours=1))
    with open_core(imported, fake_organs=True, clock=clock, to_console=False) as core:
        attach_terminal(core)
        downtime = core.heartbeat.wake_up()
        await converse(core, "搬家后还记得吗")
        await core.heartbeat.shut_down()
    assert downtime.duration == timedelta(hours=1)
    assert "搬家前说的话" in requests[-1].prompt

    moved = AureusPaths(tmp_path / "third_disk" / "moved_home")
    moved.home.parent.mkdir()
    shutil.move(imported.home, moved.home)
    with open_core(moved, fake_organs=True, clock=clock, to_console=False) as core:
        core.heartbeat.wake_up()
        contents = [record.content for record in all_records(core)]
    assert any("搬家前说的话" in content for content in contents)
