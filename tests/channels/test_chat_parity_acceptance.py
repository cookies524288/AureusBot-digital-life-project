"""从假 QQ 来信到平台响应，复核跨模块的聊天承诺。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from aureus.channels.onebot import decode_message
from aureus.common import ManualClock
from aureus.heartbeat import MediaReference, Perception
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.organs import OrganRequest, OrganResponse, ToolCall
from tests.channels.test_qq_behavior_replay import START, ReplayOrgan, open_replay
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, event
from tests.planner_fixtures import plan_response
from tests.prompt_assertions import dialogue_text

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("valid_target", [True, False])
async def test_two_speakers_quote_only_the_selected_real_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid_target: bool
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        replay.planner.plan = {"reply": False, "stay_in_conversation": True}
        await replay.receive(event(4100, 2110, "进入这一轮", group=GROUP))
        await replay.step()
        assert replay.fake.messages.empty()

        async def choose(request: OrganRequest) -> OrganResponse:
            prompt = dialogue_text(request)
            assert "七点南门" in prompt and "番茄面" in prompt
            target = next(
                row for row in replay.records() if row.metadata.get("message_id") == "4101"
            )
            return plan_response(
                request,
                {
                    "reply": True,
                    "intent": "回答集合问题",
                    "quote_source": True,
                    "target_record_id": target.id if valid_target else 999999999,
                },
            )

        monkeypatch.setattr(replay.planner, "process", choose)
        await replay.receive(
            event(4101, 2110, "七点南门集合吗？", group=GROUP),
            event(4102, 2120, "我在吃番茄面。", group=GROUP),
        )
        await replay.step()
        if valid_target:
            await asyncio.wait_for(replay.assert_sent(4101), 2)
            channel = replay.core.qq
            assert channel is not None
            await asyncio.gather(*channel._sending)
        else:
            assert not replay.replyer.calls
        assert replay.fake.messages.empty()


@pytest.mark.parametrize(
    ("kind", "available"), [("image", True), ("record", True), ("image", False)]
)
async def test_followup_can_read_only_a_description_actually_obtained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, available: bool
) -> None:
    clock = ManualClock(START)
    interpreter = ReplayOrgan("vlm" if kind == "image" else "voice", clock)
    description = "左边是银色风车" if kind == "image" else "语音里约了九点半"
    samples: list[bytes | None] = []

    async def interpret(request: OrganRequest) -> OrganResponse:
        samples.append(request.media_bytes)
        return OrganResponse(request.organ_id, description)

    async def read(_source: Perception, reference: MediaReference) -> tuple[bytes, str] | None:
        assert reference.file_id == "scoped-media-01"
        return (b"fixture", "image/png" if kind == "image" else "audio/wav") if available else None

    monkeypatch.setattr(interpreter, "process", interpret)
    async with open_replay(tmp_path, monkeypatch, clock, emoji_vlm=interpreter) as replay:
        replay.core.heartbeat.set_inbox_attachment_reader(read)
        await replay.receive(
            event(
                4601,
                2161,
                "",
                group=GROUP,
                message=[
                    {"type": kind, "data": {"file": "scoped-media-01"}},
                ],
            )
        )
        await replay.step()
        assert samples == ([b"fixture"] if available else [])
        results: list[dict] = []

        async def ask_history(request: OrganRequest) -> OrganResponse:
            if not request.tool_history:
                return OrganResponse(
                    request.organ_id,
                    "",
                    tool_calls=(ToolCall("read_platform_history", "previous_media", {"limit": 3}),),
                )
            results.append(json.loads(request.tool_history[-1].results[0].content))
            return plan_response(request, {"reply": True, "intent": "按已有说明回答，不猜测"})

        monkeypatch.setattr(replay.planner, "process", ask_history)
        await replay.receive(event(4602, 2161, "再看一下刚才那条附件？", group=GROUP))
        await replay.step()
        [page] = results
        original = next(row for row in page["messages"] if row["platform_message_id"] == "4601")
        assert (description in original["content"]) is available
        assert available or "没有已取得的说明" in original["content"]
        assert "scoped-media-01" not in str(page)
        await asyncio.wait_for(replay.assert_sent(), 2)


async def test_next_turn_hears_acknowledged_segment_but_not_declined_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, split_reply=True) as replay:
        channel = replay.core.qq
        assert channel is not None
        replay.planner.plan = {"reply": True, "intent": "回复两段"}
        replay.fake.reply_with[:] = ["ok", "failed"]

        async def reply(request: OrganRequest) -> OrganResponse:
            return OrganResponse(request.organ_id, "已经送达甲段<n>平台拒收乙段")

        monkeypatch.setattr(replay.replyer, "process", reply)
        await replay.receive(event(4201, 2111, "请说两句。", group=GROUP))
        await replay.step()
        await asyncio.wait_for(replay.fake.messages.get(), 2)
        await asyncio.gather(*channel._sending)
        clock.advance(timedelta(seconds=20))
        replay.core.heartbeat.beat()
        await asyncio.wait_for(replay.fake.messages.get(), 2)
        await asyncio.gather(*channel._sending)
        assert [row.content for row in replay.records("reply_delivered")] == ["已经送达甲段"]
        assert len(replay.records("reply_delivery_failed")) == 1

        replay.planner.plan = {"reply": False}
        await replay.receive(event(4202, 2111, "你刚才说了什么？", group=GROUP))
        await replay.step()
        prompt = dialogue_text(replay.planner.calls[-1][1])
        assert "已经送达甲段" in prompt and "平台拒收乙段" not in prompt
        assert replay.fake.messages.empty()


async def test_recall_during_reply_prevents_the_first_visible_qq_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        started, finish = asyncio.Event(), asyncio.Event()
        replay.planner.plan = {"reply": True, "intent": "回应问题"}

        async def delayed(request: OrganRequest) -> OrganResponse:
            started.set()
            await finish.wait()
            return OrganResponse(request.organ_id, "这段不应投递")

        monkeypatch.setattr(replay.replyer, "process", delayed)
        heartbeat, store = replay.core.heartbeat, replay.core.inbox_store
        assert store is not None
        key = ConversationKey("qq", str(ACCOUNT), "group", str(GROUP))
        await replay.receive(event(4301, 2112, "先别发出去", group=GROUP))
        heartbeat.beat()
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert store.find_platform_message(key, "4301") is not None
            await replay.fake.send(
                replay.remote,
                post_type="notice",
                notice_type="group_recall",
                self_id=ACCOUNT,
                group_id=GROUP,
                user_id=2112,
                operator_id=2112,
                message_id=4301,
            )
            async with asyncio.timeout(2):
                while store.find_platform_message(key, "4301") is not None:
                    await asyncio.sleep(0.01)
        finally:
            finish.set()
        await heartbeat.wait_for_thought()
        heartbeat.beat()
        channel = replay.core.qq
        assert channel is not None
        await asyncio.gather(*channel._sending)
        assert replay.fake.messages.empty() and not replay.records("reply_delivered")


@pytest.mark.parametrize("recalled", [False, True])
async def test_old_quote_reads_only_its_own_visible_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recalled: bool
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START), history_limit=1) as replay:
        store = replay.core.inbox_store
        assert store is not None
        for group, sender, content in (
            (GROUP, 2141, "原定西门的银色风车"),
            (GROUP + 1, 2143, "别群同号的红色气球"),
        ):
            parsed = decode_message(event(4401, sender, content, group=group), str(ACCOUNT))
            assert parsed is not None
            original = store.add(parsed, sender_name=f"群友{sender}")
            assert original is not None
            store.mark_read([original.id])
        if recalled:
            store.mark_recalled(ConversationKey("qq", str(ACCOUNT), "group", str(GROUP)), "4401")
        replay.planner.plan = {"reply": True, "intent": "核对引用的地点", "quote_source": True}
        await replay.receive(
            event(
                4402,
                2142,
                "",
                group=GROUP,
                message=[
                    {"type": "reply", "data": {"id": "4401"}},
                    {"type": "text", "data": {"text": "这个约定还算吗？"}},
                ],
            )
        )
        await replay.step()
        await asyncio.wait_for(replay.assert_sent(4402), 2)
        for organ in (replay.planner, replay.replyer):
            prompt = dialogue_text(organ.calls[-1][1])
            assert ("原定西门的银色风车" in prompt) is not recalled
            assert "别群同号的红色气球" not in prompt
            assert "2142" in prompt and (recalled or "2141" in prompt)


async def test_idle_mention_uses_nearby_context_without_answering_the_bystander(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        replay.planner.plan = {"reply": True, "intent": "回答刚才的集合提问", "quote_source": True}
        for payload in (
            event(4501, 2151, "七点在南门集合", group=GROUP),
            event(
                4502,
                2152,
                "",
                group=GROUP,
                message=[
                    {"type": "at", "data": {"qq": str(ACCOUNT)}},
                    {"type": "text", "data": {"text": "你也来吗？"}},
                ],
            ),
        ):
            await replay.fake.send(replay.remote, **payload)
            await asyncio.wait_for(replay.accepted.get(), 2)
        await replay.step()
        await asyncio.wait_for(replay.assert_sent(4502), 2)
        for organ in (replay.planner, replay.replyer):
            request = organ.calls[-1][1]
            assert "七点在南门集合" in dialogue_text(request)
            [identifier] = request.context["in_reply_to"]
            assert any(
                row.id == identifier and row.metadata.get("message_id") == "4502"
                for row in replay.records()
            )
        assert replay.fake.messages.empty()
