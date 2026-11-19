"""醒来时记下用户改了哪几层设定的测试（路线图 1.9，D62）。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import (
    EVENT_IDENTITY_CHANGED,
    EVENT_WOKE_UP,
    Heartbeat,
    OutgoingMessage,
    Perception,
    TrustLevel,
)
from aureus.identity import Identity, IdentityHistory, IdentityLayer
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
CHANNEL = "terminal"


def make_identity(**persona: str) -> Identity:
    return Identity.model_validate(
        {
            "facts": {"name": "小绿", "birthday": "2008-01-01"},
            "user": {"name": "阿明"},
            "persona": {"immutable": "温和。", "expression_style": "句子短。", **persona},
        }
    )


class RecordingOrgans:
    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        return OrganResponse(organ_id=request.organ_id, suggestion="嗯。")


class Rig:
    """一个数据目录；start() 每次相当于启动一次：先和 identity.toml 对一遍，再醒来。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = ManualClock(NOW)
        self.timeline = TimelineDatabase(tmp_path / "aureus.db", self.clock)
        self.timeline.open()
        self.history = IdentityHistory(tmp_path / "aureus.db", self.clock)
        self.history.open()
        self.organs = RecordingOrgans()

    def start(self, from_file: Identity, **config: Any) -> Heartbeat:
        identity = self.history.sync_from_file(from_file)
        emergency_stop = EmergencyStop(self.tmp_path / "halt.json", self.clock)
        heartbeat = Heartbeat(
            clock=self.clock,
            timeline=self.timeline,
            organs=self.organs,
            identity=identity,
            config=HeartbeatConfig(planning_enabled=False, split_reply_enabled=False, **config),
            permissions=PermissionChecker(emergency_stop),
            emergency_stop=emergency_stop,
            timezone=UTC,
            identity_history=self.history,
        )
        sent: list[OutgoingMessage] = []
        heartbeat.attach_channel(CHANNEL, sent.append)
        heartbeat.wake_up()
        return heartbeat

    def changes(self) -> list[Any]:
        return list(
            self.timeline.query(TimelineQuery(event_type=EVENT_IDENTITY_CHANGED, limit=100))
        )

    async def ask(self, heartbeat: Heartbeat) -> str:
        """说一句话，返回这次给器官的系统上下文。"""
        heartbeat.submit(
            Perception(
                CHANNEL, "local", "你好", VisibilityScope.USER_ONLY, TrustLevel.VERIFIED_USER
            )
        )
        heartbeat.beat()
        await heartbeat.wait_for_thought()
        heartbeat.beat()
        return self.organs.requests[-1].system_context

    def close(self) -> None:
        self.history.close()
        self.timeline.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


async def test_the_first_start_records_no_change(rig: Rig) -> None:
    heartbeat = rig.start(make_identity())

    assert rig.changes() == []
    assert "用户修改了你的" not in await rig.ask(heartbeat)


async def test_a_changed_layer_is_recorded_right_after_waking(rig: Rig) -> None:
    await rig.start(make_identity()).shut_down()
    rig.start(make_identity(expression_style="多用语气词。"))

    records = rig.timeline.query(TimelineQuery(limit=100))
    event_types = [record.event_type for record in records]
    assert event_types[-2:] == [EVENT_WOKE_UP, EVENT_IDENTITY_CHANGED]
    change = records[-1]
    assert change.content == "用户修改了我的设定：表达风格。"
    assert change.actor_type is ActorType.USER
    assert change.visibility is VisibilityScope.PRIVATE
    assert change.metadata["layers"] == ["expression_style"]


async def test_the_change_is_mentioned_in_the_context_for_this_run(rig: Rig) -> None:
    await rig.start(make_identity()).shut_down()
    heartbeat = rig.start(make_identity(expression_style="多用语气词。"))

    context = await rig.ask(heartbeat)

    assert "这次醒来前，用户修改了你的表达风格，上面写的是改过之后的样子。" in context
    assert "【表达风格】\n多用语气词。" in context
    assert "句子短。" not in context  # 不给旧内容


async def test_the_switch_keeps_it_out_of_the_context_but_not_the_timeline(rig: Rig) -> None:
    await rig.start(make_identity()).shut_down()
    heartbeat = rig.start(
        make_identity(expression_style="多用语气词。"), identity_changes_in_prompt=False
    )

    assert len(rig.changes()) == 1
    assert "用户修改了你的" not in await rig.ask(heartbeat)


async def test_a_change_is_recorded_only_once(rig: Rig) -> None:
    await rig.start(make_identity()).shut_down()
    await rig.start(make_identity(expression_style="多用语气词。")).shut_down()
    heartbeat = rig.start(make_identity(expression_style="多用语气词。"))

    assert len(rig.changes()) == 1
    assert "用户修改了你的" not in await rig.ask(heartbeat)


async def test_several_layers_are_named_together(rig: Rig) -> None:
    await rig.start(make_identity()).shut_down()
    rig.start(
        make_identity(mutable="想多交朋友。", expression_style="多用语气词。", immutable="倔强。")
    )

    assert rig.changes()[0].content == "用户修改了我的设定：不变人格、可变人格、表达风格。"


async def test_a_change_not_yet_recorded_is_caught_up_on_the_next_start(rig: Rig) -> None:
    """上次和 identity.toml 对完、还没醒来就停了：这次醒来补记，不会漏。"""
    await rig.start(make_identity()).shut_down()
    rig.history.sync_from_file(make_identity(expression_style="多用语气词。"))  # 没有醒来

    rig.start(make_identity(expression_style="多用语气词。"))

    assert [r.content for r in rig.changes()] == ["用户修改了我的设定：表达风格。"]


async def test_an_unreadable_cursor_starts_over_instead_of_missing_changes(rig: Rig) -> None:
    await rig.start(make_identity()).shut_down()
    rig.history.sync_from_file(make_identity(expression_style="多用语气词。"))
    rig.timeline.append(
        EVENT_IDENTITY_CHANGED,
        "坏掉的记录",
        ActorType.USER,
        visibility=VisibilityScope.PRIVATE,
        metadata={"up_to_version_id": "不是数字"},
    )

    rig.start(make_identity(expression_style="多用语气词。"))

    assert rig.changes()[-1].content == "用户修改了我的设定：表达风格。"


# ---------- 运行中在网页上改设定（路线图 1.10，D63 第 4 条） ----------

NEW_STYLE = "多用语气词。"
STYLE = IdentityLayer.EXPRESSION_STYLE


def edit_live(heartbeat: Heartbeat, rig: Rig, layers: list[IdentityLayer]) -> Identity:
    new = make_identity(expression_style=NEW_STYLE)
    version = rig.history.versions(None, limit=1)[0].id
    heartbeat.apply_identity_edit(new, layers, up_to_version_id=version, via="web")
    return new


async def test_a_live_edit_takes_effect_on_the_next_reply(rig: Rig) -> None:
    heartbeat = rig.start(make_identity())
    edit_live(heartbeat, rig, [STYLE])

    context = await rig.ask(heartbeat)

    assert f"【表达风格】\n{NEW_STYLE}" in context
    assert "句子短。" not in context
    assert "刚才，用户修改了你的表达风格，上面写的是改过之后的样子。" in context


async def test_a_live_edit_is_mentioned_only_in_the_next_reply(rig: Rig) -> None:
    heartbeat = rig.start(make_identity())
    edit_live(heartbeat, rig, [STYLE])
    await rig.ask(heartbeat)

    second = await rig.ask(heartbeat)

    assert "用户修改了你的" not in second
    assert f"【表达风格】\n{NEW_STYLE}" in second  # 新设定一直生效，只是不再重复说


async def test_a_live_edit_is_recorded_with_where_it_came_from(rig: Rig) -> None:
    heartbeat = rig.start(make_identity())
    edit_live(heartbeat, rig, [STYLE])

    record = rig.changes()[-1]
    assert record.content == "用户修改了我的设定：表达风格。"
    assert record.actor_type is ActorType.USER
    assert record.visibility is VisibilityScope.PRIVATE
    assert record.metadata["via"] == "web"
    assert record.metadata["layers"] == ["expression_style"]


async def test_the_switch_keeps_a_live_edit_out_of_the_context_but_not_the_timeline(
    rig: Rig,
) -> None:
    heartbeat = rig.start(make_identity(), identity_changes_in_prompt=False)
    edit_live(heartbeat, rig, [STYLE])

    context = await rig.ask(heartbeat)

    assert len(rig.changes()) == 1
    assert "用户修改了你的" not in context
    assert f"【表达风格】\n{NEW_STYLE}" in context  # 开关只管说不说，不管生效不生效


async def test_a_lock_only_edit_changes_the_identity_but_records_nothing(rig: Rig) -> None:
    heartbeat = rig.start(make_identity())
    edit_live(heartbeat, rig, [])

    context = await rig.ask(heartbeat)

    assert rig.changes() == []
    assert "用户修改了你的" not in context


async def test_a_live_edit_is_not_announced_again_on_the_next_start(rig: Rig) -> None:
    heartbeat = rig.start(make_identity())
    new = make_identity(expression_style=NEW_STYLE)
    rig.history.sync_from_file(new)  # 网页写回文件、记了版本，就是这一步
    version = rig.history.versions(None, limit=1)[0].id
    heartbeat.apply_identity_edit(new, [STYLE], up_to_version_id=version, via="web")
    await heartbeat.shut_down()

    restarted = rig.start(new)

    assert len(rig.changes()) == 1
    assert "用户修改了你的" not in await rig.ask(restarted)
