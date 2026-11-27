"""验证实际使用的本地词典、群查询边界、持久化提醒和文件授权。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat.chat_assets import ApprovedAssets
from aureus.heartbeat.chat_plugins import ChatPluginTools, ChatRequest
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.perception import MessageRoute, Perception, TrustLevel, VisibilityScope

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
BOT, GROUP, OWNER = "100", "200", "300"
GROUP_KEY = ConversationKey("qq", BOT, "group", GROUP)
OWNER_KEY = ConversationKey("qq", BOT, "private", OWNER)


def received(store: InboxStore, *, owner: bool = False, message: str = "11") -> Perception:
    key = OWNER_KEY if owner else GROUP_KEY
    source = Perception(
        "qq",
        OWNER if owner else "400",
        "假来信",
        VisibilityScope.USER_ONLY if owner else VisibilityScope.GROUP,
        TrustLevel.VERIFIED_USER if owner else TrustLevel.VERIFIED_CONTACT,
        MessageRoute(BOT, key.kind, key.recipient_id, message),
    )
    row = store.add(source, conversation_name="合成同名群")
    assert row is not None
    store.mark_read([row.id])
    return source


def plugins(store: InboxStore, clock: ManualClock) -> ChatPluginTools:
    result = ChatPluginTools(
        store, clock, HeartbeatConfig(chat_meme_glossary={"蓝月亮针": "从合成词库取得的释义"})
    )
    result.allows = lambda key: key in (GROUP_KEY, OWNER_KEY)
    result.is_owner = lambda account: account == OWNER
    return result


@pytest.mark.asyncio
async def test_groups_limit_disclosure_and_dictionary_has_source(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inbox = InboxStore(tmp_path / "db", clock)
    chat = plugins(inbox, clock)
    member, owner = received(inbox), received(inbox, owner=True)
    called: list[Perception] = []

    async def actual(source: Perception) -> list[tuple[str, str]]:
        called.append(source)
        return [(GROUP, "合成同名群"), (GROUP, "别名"), ("999", "主人可见群")]

    chat.groups = actual
    for query in ("", "同名", "主人可见群", "999"):
        answer = await chat.run(member, ChatRequest("groups", query), writable=True)
        assert "不能确认" in answer and "999" not in answer and "主人可见群" not in answer
    for query in (GROUP, "合成同名群"):
        assert "是，我在" in await chat.run(member, ChatRequest("groups", query), writable=True)
    assert not called
    answer = json.loads(await chat.run(owner, ChatRequest("groups", ""), writable=True))
    assert called == [owner] and answer["total"] == 2 and ["999", "主人可见群"] in answer["groups"]
    assert "从合成词库取得的释义" in await chat.run(
        member, ChatRequest("meme", "蓝月亮针是什么意思"), writable=True
    )
    assert "没查到" in await chat.run(member, ChatRequest("meme", "未知词"), writable=True)
    chat.is_owner = lambda _: False
    assert "groups" not in chat.operations(owner, writable=True)
    assert "groups" not in chat.operations(
        replace(member, trust=TrustLevel.VERIFIED_USER), writable=True
    )


@pytest.mark.asyncio
async def test_due_and_nudge_restore_use_only_authorized_originals(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inbox = InboxStore(tmp_path / "db", clock)
    chat = plugins(inbox, clock)
    member, owner = received(inbox), received(inbox, owner=True)
    reminder = ChatRequest(
        "create_reminder", "交设计草图", 30, target_kind="group", target_id=GROUP
    )
    nudge = replace(reminder, operation="owner_nudge", text="有机会问截止时间", seconds=40)
    assert "未获准" in await chat.run(member, reminder, writable=True)
    assert "未获准" in await chat.run(member, nudge, writable=True)
    assert "未获准" in await chat.run(owner, replace(nudge, target_id="999"), writable=True)
    assert "登记" in await chat.run(owner, reminder, writable=True)
    assert "登记" in await chat.run(owner, nudge, writable=True)
    resumed = plugins(InboxStore(tmp_path / "db", clock), clock)
    assert resumed.take_due() is None
    assert resumed.nudge(OWNER_KEY) is None
    active = resumed.nudge(GROUP_KEY)
    assert active and active.note == nudge.text
    clock.advance(timedelta(seconds=30))
    due = resumed.take_due()
    assert due and due[0].note == reminder.text and due[1].perception == member
    assert resumed.take_due() is None and resumed.nudge(GROUP_KEY) == active
    resumed.consume_nudge(OWNER_KEY, active.id)
    assert resumed.nudge(GROUP_KEY) == active
    resumed.is_owner = lambda _: False
    assert resumed.nudge(GROUP_KEY) is None
    clock.advance(timedelta(seconds=10))
    resumed.is_owner = lambda _: True
    assert resumed.nudge(GROUP_KEY) is None


def test_asset_original_type_name_route_revocation_and_size(tmp_path: Path) -> None:
    assets = ApprovedAssets(tmp_path)
    directory = tmp_path / BOT / "group" / GROUP
    directory.mkdir(parents=True)
    (directory / "说明.txt").write_text("只给本群", encoding="utf-8")
    Image.new("RGB", (2, 2), "yellow").save(directory / "示意图.png")
    (directory / "替身.png").write_bytes(b"not an image")
    (directory / "过大.txt").write_bytes(b"x" * 720_000)
    assert assets.read(GROUP_KEY, "说明.txt") == ("说明.txt", "只给本群".encode())
    assert assets.read(GROUP_KEY, "示意图.png") == (
        "示意图.png",
        (directory / "示意图.png").read_bytes(),
    )
    for invalid in (
        "替身.png",
        "过大.txt",
        "../说明.txt",
        str(directory / "说明.txt"),
        "不存在.txt",
    ):
        assert assets.read(GROUP_KEY, invalid) is None
    for other in (
        OWNER_KEY,
        replace(GROUP_KEY, account_id="999"),
        replace(GROUP_KEY, temporary_group_id="5"),
    ):
        assert not assets.names(other) and assets.read(other, "说明.txt") is None
    (directory / "说明.txt").unlink()
    assert not assets.allows(GROUP_KEY, "说明.txt")
