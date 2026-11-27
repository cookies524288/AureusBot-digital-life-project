"""从真实入箱通知、规划工具到回环 QQ，验证表情时序与复读。"""

import asyncio
from base64 import b64encode
from datetime import timedelta
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.organs import OrganRequest, OrganResponse
from tests.channels.test_qq_behavior_replay import START, Replay, open_replay
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, event

pytestmark = pytest.mark.asyncio
MEDIA = b"GIF89a approved synthetic sticker"
IMAGE = {"type": "image", "data": {"file": "base64://" + b64encode(MEDIA).decode(), "sub_type": 1}}
HERE = ConversationKey("qq", str(ACCOUNT), "group", str(GROUP))


async def notify(replay: Replay, number: int, sender: int, parts: list[dict]) -> None:
    await replay.fake.send(replay.remote, **event(number, sender, "", group=GROUP, message=parts))
    incoming = await asyncio.wait_for(replay.accepted.get(), 2)
    assert incoming.perception.route is not None and incoming.perception.route.message_id == str(
        number
    )


async def ask(replay: Replay, number: int) -> None:
    await notify(
        replay,
        number,
        2291,
        [
            {"type": "at", "data": {"qq": str(ACCOUNT)}},
            {"type": "text", "data": {"text": "我们完成啦！"}},
        ],
    )


async def settle(replay: Replay) -> None:
    channel = replay.core.qq
    assert channel is not None
    await asyncio.gather(*channel._sending)


async def frame(replay: Replay) -> list[dict]:
    message = await asyncio.wait_for(replay.fake.messages.get(), 2)
    assert message["message_type"] == "group" and message["group_id"] == str(GROUP)
    await settle(replay)
    return message["message"]


@pytest.mark.parametrize(
    ("explicit", "stop"),
    [
        (False, ""),
        (True, ""),
        (False, "neutral"),
        (True, "failure"),
        (False, "failure"),
        (True, "expired"),
        (True, "arrival"),
        (True, "recall"),
        (True, "revoked"),
    ],
)
async def test_followup_waits_for_all_text_receipts_and_valid_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool, stop: str
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, split_reply=True) as replay:
        store, inbox, channel = replay.core.emoji_store, replay.core.inbox_store, replay.core.qq
        assert store is not None and inbox is not None and channel is not None
        store.collect(MEDIA, description="欢呼", register=True)
        replay.fake.receipt_ids[:] = [7101, 7102, 7103]
        if stop == "failure":
            replay.fake.reply_with[:] = ["ok", "failed"]
        replay.planner.plan = {"reply": True, "intent": "回应群友"}
        if explicit:
            replay.planner.plan["select_emoji"] = {"emotion": "欢呼", "before_text": True}
        select, release = asyncio.Event(), asyncio.Event()
        choices: list[str] = []
        opening = "计划已经记下。" if stop == "neutral" else "太好了！"

        async def respond(request: OrganRequest) -> OrganResponse:
            if request.context["task"] == "emoji":
                choices.append(request.prompt)
                select.set()
                await release.wait()
                return OrganResponse(request.organ_id, '{"selected":1}')
            if explicit:
                await select.wait()
            return OrganResponse(request.organ_id, opening + "<n>第二段。")

        monkeypatch.setattr(replay.replyer, "process", respond)
        await ask(replay, 6100)
        await replay.step()
        assert await frame(replay) == [{"type": "text", "data": {"text": opening}}]
        assert replay.fake.messages.empty() and (bool(choices) is explicit)
        assert [row.metadata["message_id"] for row in replay.records("reply_delivered")] == ["7101"]
        clock.advance(timedelta(seconds=9))
        replay.core.heartbeat.beat()
        assert await frame(replay) == [{"type": "text", "data": {"text": "第二段。"}}]
        if explicit or not stop:
            await asyncio.wait_for(select.wait(), 2)
        if stop == "expired":
            clock.advance(timedelta(minutes=1))
        elif stop == "arrival":
            await notify(replay, 6101, 2292, [{"type": "text", "data": {"text": "换个话题"}}])
        elif stop == "recall":
            await replay.fake.send(
                replay.remote,
                post_type="notice",
                notice_type="group_recall",
                self_id=ACCOUNT,
                group_id=GROUP,
                user_id=2291,
                operator_id=2291,
                message_id=6100,
            )
            async with asyncio.timeout(2):
                while inbox.find_platform_message(HERE, "6100") is not None:
                    await asyncio.sleep(0.005)
        elif stop == "revoked":
            monkeypatch.setattr(channel, "_allows_route", lambda _route: False)
        release.set()
        heart = replay.core.heartbeat
        if stop:
            heart.beat()
            async with asyncio.timeout(2):
                while heart._sticker_followups.active:
                    await asyncio.sleep(0.005)
            await settle(replay)
        else:
            assert await frame(replay) == [IMAGE]
        assert len(choices) == int(explicit or stop not in ("neutral", "failure"))
        assert store.browse("", 0, 10)[0].use_count == int(not stop)
        assert replay.fake.messages.empty()
        assert [row.metadata["message_id"] for row in replay.records("reply_delivered")] == (
            ["7101"]
            if stop == "failure"
            else ["7101", "7102"]
            if stop
            else ["7101", "7102", "7103"]
        )


@pytest.mark.parametrize("state", ["ok", "empty", "banned", "failed"])
async def test_sticker_alone_has_no_text_frame_or_phantom_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        store, inbox = replay.core.emoji_store, replay.core.inbox_store
        assert store is not None and inbox is not None
        if state != "empty":
            entry = store.collect(MEDIA, description="欢呼", register=True)
            assert entry.digest is not None
            if state == "banned":
                store.set_status(entry.digest, is_registered=True, is_banned=True)
        replay.planner.plan = {"reply": True, "intent": "用一张图回应", "reply_mode": "sticker"}
        replay.fake.receipt_ids[:] = [7201]
        if state == "failed":
            replay.fake.reply_with[:] = ["failed"]
        calls = []

        async def respond(request: OrganRequest) -> OrganResponse:
            calls.append(request.context["task"])
            return OrganResponse(request.organ_id, '{"selected":1}')

        monkeypatch.setattr(replay.replyer, "process", respond)
        await ask(replay, 6200)
        await replay.step()
        if state in ("ok", "failed"):
            assert await frame(replay) == [IMAGE]
        assert calls == (["emoji"] if state in ("ok", "failed") else [])
        assert replay.fake.messages.empty()
        assert bool(inbox.find_platform_message(HERE, "7201")) is (state == "ok")
        assert len(replay.records("reply_delivered")) == int(state == "ok")
        assert sum(row.use_count for row in store.browse("", 0, 10)) == int(state == "ok")


@pytest.mark.parametrize("stop", ["permission", "halt", "shutdown"])
async def test_discard_after_text_generation_cancels_an_unclaimed_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        heart, store, channel = replay.core.heartbeat, replay.core.emoji_store, replay.core.qq
        assert store and channel
        store.collect(MEDIA, description="欢呼", register=True)
        replay.planner.plan = {"reply": True, "intent": "回应", "select_emoji": {"emotion": "高兴"}}
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def respond(request: OrganRequest) -> OrganResponse:
            if request.context["task"] == "emoji":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            await started.wait()
            return OrganResponse(request.organ_id, "太好了！")

        monkeypatch.setattr(replay.replyer, "process", respond)
        await ask(replay, 6211)
        heart.beat()
        await heart.wait_for_thought()
        if stop == "permission":
            monkeypatch.setattr(channel, "_allows_route", lambda _route: False)
        elif stop == "halt":
            heart.halt("合成演练")
        else:
            heart.request_stop()
        heart.beat()
        if stop == "shutdown":
            await heart.shut_down()
        await asyncio.wait_for(cancelled.wait(), 2)
        await settle(replay)
        assert not replay.records("reply_delivered") and replay.fake.messages.empty()


async def enter(replay: Replay) -> None:
    replay.planner.plan = {"reply": False, "stay_in_conversation": True}
    await ask(replay, 6300)
    await replay.step()


@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        ("sticker_duel", "sticker"),
        ("sticker_duel", "photo"),
        ("repeat", "text"),
        ("repeat", "face"),
        ("repeat", "image"),
        ("repeat", "banned"),
        ("repeat", "alone"),
    ],
)
async def test_play_with_other_members_is_bounded_by_actual_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, kind: str
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock) as replay:
        store = replay.core.emoji_store
        assert store is not None
        entry = store.collect(MEDIA, description="快乐的合成表情", register=True)
        assert entry.digest is not None
        if kind == "banned":
            store.set_status(entry.digest, is_registered=True, is_banned=True)

        async def read(_source, attachment):
            assert attachment.file_id == "same-file"
            return MEDIA, "image/gif"

        replay.core.heartbeat.set_inbox_attachment_reader(read)
        choices = []

        async def select(request: OrganRequest) -> OrganResponse:
            choices.append(request.context["task"])
            return OrganResponse(request.organ_id, '{"selected":1}')

        monkeypatch.setattr(replay.replyer, "process", select)
        replay.fake.receipt_ids[:] = [7301, 7302]
        await enter(replay)
        text = {"type": "text", "data": {"text": "前排围观"}}
        face = {"type": "face", "data": {"id": "14"}}
        photo = {"type": "image", "data": {"file": "same-file", "sub_type": int(kind == "sticker")}}
        part = text if kind in ("text", "alone") else face if kind == "face" else photo
        replay.planner.plan = {
            "reply": True,
            "intent": "跟一次",
            "reply_mode": mode,
            "stay_in_conversation": True,
        }
        if mode == "repeat":
            await notify(replay, 6301, 2293, [part])
            await replay.step()
            assert replay.fake.messages.empty()
        await notify(replay, 6302, 2293 if kind == "alone" else 2294, [part])
        await replay.step()
        good = kind in ("sticker", "text", "face", "image")
        if good:
            assert await frame(replay) == [
                text if kind == "text" else face if kind == "face" else IMAGE
            ]
        await notify(replay, 6303, 2293, [part])
        await replay.step()
        await settle(replay)
        assert replay.fake.messages.empty()
        assert len(replay.records("reply_delivered")) == int(good)
        assert store.browse("", 0, 10)[0].use_count == int(kind in ("sticker", "image"))
        assert choices == (["emoji"] if kind == "sticker" else [])


async def test_refused_repeat_does_not_cool_down_a_future_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock) as replay:
        await enter(replay)
        replay.planner.plan = {
            "reply": True,
            "intent": "接龙一次",
            "reply_mode": "repeat",
            "stay_in_conversation": True,
        }
        replay.fake.reply_with[:] = ["failed", "ok", "ok"]
        replay.fake.receipt_ids[:] = [7401, 7402]
        part = {"type": "text", "data": {"text": "大家都到齐了"}}
        for number, sender, transmitted in [
            (6311, 2291, False),
            (6312, 2292, True),
            (6313, 2293, True),
            (6314, 2291, False),
            (6315, 2293, True),
        ]:
            if number == 6315:
                clock.advance(timedelta(minutes=3))
            await notify(replay, number, sender, [part])
            await replay.step()
            if transmitted:
                assert await frame(replay) == [part]
            assert replay.fake.messages.empty()
        assert [row.metadata["message_id"] for row in replay.records("reply_delivered")] == [
            "7401",
            "7402",
        ]
        assert len(replay.records("reply_delivery_failed")) == 1
