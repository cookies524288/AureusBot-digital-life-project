"""用合成群消息、假器官和回环 OneBot 验证完整表情回复链路。"""

import asyncio
from base64 import b64encode
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import InboxMessage
from aureus.organs import OrganRequest, OrganResponse
from tests.channels.test_qq_behavior_replay import START, Replay, ReplayOrgan, open_replay
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, event
from tests.heartbeat.test_emoji_selection import synthetic_emoji

pytestmark = pytest.mark.asyncio


async def receive_mention(
    replay: Replay, monkeypatch: pytest.MonkeyPatch, message_id: int, text: str
) -> None:
    channel = replay.core.qq
    assert channel is not None
    owner = replay.core.people.find("web", "local")
    assert owner is not None and owner.is_user
    replay.core.people.bind_verified_account(owner, "qq", "2100", None)
    received: asyncio.Queue[InboxMessage] = asyncio.Queue()

    def observe(message: InboxMessage) -> None:
        replay.core.heartbeat.on_inbox_message(message)
        received.put_nowait(message)

    monkeypatch.setattr(channel, "_on_inbox_message", observe)
    segments = [
        {"type": "at", "data": {"qq": str(ACCOUNT)}},
        {"type": "text", "data": {"text": text}},
    ]
    payload = event(message_id, 2100, "", group=GROUP)
    payload["message"] = segments
    await replay.fake.send(replay.remote, **payload)
    observed = await asyncio.wait_for(received.get(), timeout=2)
    assert observed.perception.source_message_id == f"{ACCOUNT}:{message_id}"


@pytest.mark.parametrize(("before_text", "accepted"), [(True, True), (False, False)])
@pytest.mark.parametrize("visual", [False, True])
async def test_qq_emoji_reply_uses_final_plan_and_counts_only_platform_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before_text: bool, accepted: bool, visual: bool
) -> None:
    clock = ManualClock(START)
    vlm = ReplayOrgan("vlm", clock) if visual else None
    async with open_replay(
        tmp_path, monkeypatch, clock, qq_test_group=str(GROUP), emoji_vlm=vlm
    ) as replay:
        store, channel = replay.core.emoji_store, replay.core.qq
        assert store is not None and channel is not None
        assert replay.core.settings.runtime.heartbeat.qq_emoji_selection_enabled
        models = replay.core.models
        assert models is not None and "text_embedding" not in models.available_organs
        media = synthetic_emoji("red") if visual else b"GIF89a synthetic reply"
        store.collect(media, description="高兴地挥手", register=True)
        replay.planner.plan = {
            "reply": True,
            "intent": "回应群友的招呼",
            "select_emoji": {"emotion": "高兴", "before_text": before_text},
        }
        requests: list[OrganRequest] = []
        original_process = replay.replyer.process

        async def reply(request: OrganRequest) -> OrganResponse:
            if request.context.get("task") != "emoji":
                return await original_process(request)
            requests.append(request)
            return OrganResponse("replyer", '{"selected": 1}')

        async def choose_visually(request: OrganRequest) -> OrganResponse:
            assert request.context["task"] == "emoji_visual_selection"
            assert request.media_type == "image/png" and request.media_bytes != media
            requests.append(request)
            return OrganResponse("vlm", '{"emoji_index": 1}')

        monkeypatch.setattr(replay.replyer, "process", reply)
        if vlm is not None:
            monkeypatch.setattr(vlm, "process", choose_visually)
        if not accepted:
            replay.fake.reply_with[:] = ["ok", "failed"]

        await receive_mention(replay, monkeypatch, 31, "合成群友打招呼")
        await replay.step()
        first = await asyncio.wait_for(replay.fake.messages.get(), timeout=2)
        second = await asyncio.wait_for(replay.fake.messages.get(), timeout=2)
        await asyncio.gather(*channel._sending)

        [selection] = requests
        assert "合成群友打招呼" in selection.prompt
        assert ("高兴地挥手" in selection.prompt) is not visual
        image = {
            "type": "image",
            "data": {"file": "base64://" + b64encode(media).decode(), "sub_type": 1},
        }
        text = {"type": "text", "data": {"text": "收到。"}}
        parts = (image, text) if before_text else (text, image)
        for frame, part in zip((first, second), parts, strict=True):
            assert frame["group_id"] == str(GROUP)
            assert frame["message"] == [part]
        assert store.browse("", 0, 10)[0].use_count == int(accepted)
        assert replay.fake.messages.empty()


async def test_qq_emoji_reply_without_candidates_sends_text_without_selecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        store, channel = replay.core.emoji_store, replay.core.qq
        assert store is not None and channel is not None
        blocked = store.collect(b"GIF89a blocked", description="笑", register=True)
        assert blocked.digest is not None
        store.set_status(blocked.digest, is_registered=True, is_banned=True)
        replay.planner.plan = {
            "reply": True,
            "intent": "回应群友",
            "select_emoji": {"emotion": "高兴"},
        }

        await receive_mention(replay, monkeypatch, 32, "合成群消息")
        await replay.step()
        await asyncio.wait_for(replay.assert_sent(), timeout=2)
        await asyncio.gather(*channel._sending)

        assert all(request.context.get("task") != "emoji" for _, request, _ in replay.replyer.calls)
        assert store.browse("", 0, 10)[0].use_count == 0
        assert replay.fake.messages.empty()
