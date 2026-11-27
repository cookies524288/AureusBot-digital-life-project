"""用独立的平台原件、人物资料和假回执验收聊天上下文。"""

import asyncio
import re
from pathlib import Path

import pytest

from aureus.attention import SummaryOutcome, route_scope
from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.memory import HistoricalMemory, MemorySource
from aureus.organs import OrganRequest, OrganResponse
from aureus.people import HistoricalKind, HistoricalRecord
from aureus.timeline import ActorType, VisibilityScope
from tests.channels.test_qq_behavior_replay import START, open_replay
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, event
from tests.planner_fixtures import plan_response
from tests.prompt_assertions import dialogue_text

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("review", [False, True])
async def test_selected_person_and_self_recall_follow_a_revised_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review: bool
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        people, memory, channel = replay.core.people, replay.core.memory, replay.core.qq
        assert memory is not None and channel is not None
        for account, nickname, detail in (
            (2211, "合成甲", "银色风车"),
            (2212, "合成乙", "蓝色雨靴"),
        ):
            friend = people.meet("qq", str(account))
            people.publish_history(
                [
                    HistoricalRecord(
                        instance="fixture",
                        kind=HistoricalKind.PROFILE,
                        source_key=str(account),
                        account=("qq", str(account)),
                        name=nickname,
                        impression=detail,
                    )
                ]
            )
            memory.publish(
                [
                    HistoricalMemory(
                        MemorySource("fixture", "memory", "paragraphs", (account,)),
                        "喜欢的物件是" + detail,
                        person_key=f"aureus:{friend.id}",
                    )
                ]
            )
        replay.planner.plan = {"reply": False, "stay_in_conversation": True}
        await replay.receive(event(5000, 2213, "进来聊聊", group=GROUP))
        await replay.step()
        choices: list[int] = []
        prompts: list[str] = []

        async def choose(request: OrganRequest) -> OrganResponse:
            assert "银色风车" not in dialogue_text(request) and "蓝色雨靴" not in dialogue_text(
                request
            )
            number = "5001" if not choices else "5002"
            chosen = next(
                row.id for row in replay.records() if row.metadata.get("message_id") == number
            )
            choices.append(chosen)
            return plan_response(
                request,
                {
                    "reply": True,
                    "intent": "回答当前发言者",
                    "target_record_id": chosen,
                    "quote_source": False,
                },
            )

        async def respond(request: OrganRequest) -> OrganResponse:
            prompts.append(dialogue_text(request))
            assert (
                "你还不认识对方" not in request.system_context
                and "不是你的用户" in request.system_context
            )
            if review and len(prompts) == 1:
                match = re.search(r"reply_review_[a-z0-9]+", dialogue_text(request))
                assert match is not None
                tag = match[0]
                return OrganResponse(request.organ_id, f"<{tag}>银色风车这条应当重新想想</{tag}>")
            return OrganResponse(request.organ_id, "记得这件事。")

        monkeypatch.setattr(replay.planner, "process", choose)
        monkeypatch.setattr(replay.replyer, "process", respond)
        await replay.receive(
            event(5001, 2211, "还记得我喜欢什么吗？", group=GROUP),
            event(5002, 2212, "我也想问，还记得我喜欢什么吗？", group=GROUP),
        )
        await replay.step()
        frame = await asyncio.wait_for(replay.fake.messages.get(), 2)
        await asyncio.gather(*channel._sending)
        assert frame["message"] == [{"type": "text", "data": {"text": "记得这件事。"}}]
        assert len(prompts) == len(choices) == 1 + int(review)
        assert "喜欢的物件是银色风车" in prompts[0] and "蓝色雨靴" not in prompts[0]
        if review:
            assert "喜欢的物件是蓝色雨靴" in prompts[1] and "银色风车" not in prompts[1]
        assert replay.records()[-1].metadata["in_reply_to"] == [choices[-1]]
        assert replay.fake.messages.empty()


async def test_recalled_delivery_drops_from_prompt_focus_and_saved_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock) as replay:
        store, channel, heartbeat = replay.core.inbox_store, replay.core.qq, replay.core.heartbeat
        assert store is not None and channel is not None
        key = ConversationKey("qq", str(ACCOUNT), "group", str(GROUP))
        replay.fake.receipt_ids[:] = [7401, 7402]
        replay.planner.plan = {"reply": True, "intent": "回应群友"}
        seen: list[str] = []

        async def respond(request: OrganRequest) -> OrganResponse:
            seen.append(dialogue_text(request))
            return OrganResponse(
                request.organ_id, "紫色的合成门牌" if len(seen) == 1 else "原件不可见。"
            )

        monkeypatch.setattr(replay.replyer, "process", respond)
        await replay.receive(event(5100, 2251, "门牌是什么颜色？", group=GROUP))
        await replay.step()
        await asyncio.wait_for(replay.fake.messages.get(), 2)
        await asyncio.gather(*channel._sending)
        [delivered] = replay.records("reply_delivered")
        assert delivered.metadata["message_id"] == "7401"
        working = heartbeat._working_memory
        assert working is not None
        scope = route_scope(
            VisibilityScope.GROUP, key.platform, key.account_id, key.kind, key.recipient_id, None
        )
        working.finish(
            SummaryOutcome(
                delivered.id,
                {scope: "紫色的合成门牌"},
                revisions=((scope, store.history_revision(key)),),
            ),
            clock.now(),
        )
        assert "紫色的合成门牌" in working.context(owner=False, route=scope, exclude=set())
        await replay.fake.send(
            replay.remote,
            post_type="notice",
            notice_type="group_recall",
            self_id=ACCOUNT,
            group_id=GROUP,
            user_id=ACCOUNT,
            operator_id=ACCOUNT,
            message_id=7401,
        )
        async with asyncio.timeout(2):
            while store.find_platform_message(key, "7401") is not None:
                await asyncio.sleep(0.005)
        assert "紫色的合成门牌" not in working.context(owner=False, route=scope, exclude=set())
        await replay.receive(event(5101, 2251, "你能看到你刚才那句吗？", group=GROUP))
        await replay.step()
        await asyncio.wait_for(replay.fake.messages.get(), 2)
        assert "紫色的合成门牌" not in dialogue_text(replay.planner.calls[-1][1])
        assert "紫色的合成门牌" not in seen[-1]


@pytest.mark.parametrize("legacy_bucket", ["用户私聊", "exact_route"])
async def test_recalled_owner_message_stays_out_of_unversioned_summary_on_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_bucket: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        peer = 2257
        owner = replay.core.people.find("web", "local")
        store, working = replay.core.inbox_store, replay.core.heartbeat._working_memory
        assert owner and store and working
        replay.core.people.bind_verified_account(owner, "qq", str(peer), None)
        await replay.receive(event(5110, peer, "需要收回之前提过的位置"))
        await replay.step()
        key = ConversationKey("qq", str(ACCOUNT), "private", str(peer))
        scope = route_scope(
            VisibilityScope.USER_ONLY, "qq", key.account_id, "private", str(peer), None
        )
        source = next(row for row in replay.records() if row.metadata.get("message_id") == "5110")
        old = "只有旧摘要写下的紫门编号"
        replay.core.timeline.append(
            "working_memory_checkpoint",
            "旧格式摘要",
            ActorType.ME,
            metadata={
                "through": source.id,
                "summaries": {scope if legacy_bucket == "exact_route" else legacy_bucket: old},
            },
        )
        working.restore()
        await replay.fake.send(
            replay.remote,
            post_type="notice",
            notice_type="friend_recall",
            self_id=ACCOUNT,
            user_id=peer,
            message_id=5110,
        )
        async with asyncio.timeout(2):
            while store.find_platform_message(key, "5110") is not None:
                await asyncio.sleep(0.005)
        working.restore()
        replay.planner.plan = {"reply": True, "intent": "只根据仍有效的资料答话"}
        await replay.receive(event(5111, peer, "刚才提过什么？"))
        await replay.step()
        await asyncio.wait_for(replay.fake.messages.get(), 2)
        for organ in (replay.planner, replay.replyer):
            prompt = dialogue_text(organ.calls[-1][1])
            assert old not in prompt and "需要收回之前提过的位置" not in prompt


@pytest.mark.parametrize("invalid", ["missing", "other_group", "at_other"])
async def test_multi_speaker_reply_never_guesses_an_unsafe_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        replay.planner.plan = {"reply": False, "stay_in_conversation": False}
        await replay.receive(event(5300, 2271, "另一群的候选", group=GROUP + 1))
        await replay.step()
        other = next(row.id for row in replay.records() if row.metadata.get("message_id") == "5300")
        replay.planner.plan = {"reply": False, "stay_in_conversation": True}
        await replay.receive(event(5301, 2272, "进入本群", group=GROUP))
        await replay.step()

        async def choose(request: OrganRequest) -> OrganResponse:
            fields = {"reply": True, "intent": "回答这个问题", "quote_source": True}
            if invalid == "other_group":
                fields["target_record_id"] = other
            elif invalid == "at_other":
                fields["target_record_id"] = request.context["in_reply_to"][0]
            return plan_response(request, fields)

        monkeypatch.setattr(replay.planner, "process", choose)
        await replay.receive(
            event(
                5302,
                2273,
                "",
                group=GROUP,
                message=[
                    {"type": "at", "data": {"qq": "2272"}},
                    {"type": "text", "data": {"text": "你几点来？"}},
                ],
            ),
            event(5303, 2272, "我自己回答就好", group=GROUP),
        )
        await replay.step()
        assert not replay.replyer.calls and replay.fake.messages.empty()


@pytest.mark.parametrize("original", ["own", "other", "other_group"])
async def test_quote_wakes_only_for_actual_own_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        channel = replay.core.qq
        assert channel is not None
        replay.planner.plan = {"reply": True, "intent": "回应", "quote_source": True}
        replay.fake.receipt_ids[:] = [7500, 7501]
        await replay.receive(event(5200, 2261, "开头", group=GROUP))
        await replay.step()
        await asyncio.wait_for(replay.assert_sent(5200), 2)
        await asyncio.gather(*channel._sending)
        group = GROUP + 1 if original == "other_group" else GROUP
        quoted = 5200 if original == "other" else 7500
        await replay.fake.send(
            replay.remote,
            **event(
                5201,
                2262,
                "",
                group=group,
                message=[
                    {"type": "reply", "data": {"id": str(quoted)}},
                    {"type": "text", "data": {"text": "刚才那句呢？"}},
                ],
            ),
        )
        await asyncio.wait_for(replay.accepted.get(), 2)
        await replay.step()
        if original == "own":
            await asyncio.wait_for(replay.assert_sent(5201), 2)
        assert len(replay.replyer.calls) == (2 if original == "own" else 1)
        assert replay.fake.messages.empty()


async def test_quote_in_another_group_waits_for_the_active_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        heartbeat, channel = replay.core.heartbeat, replay.core.qq
        assert channel is not None
        replay.planner.plan = {"reply": True, "intent": "回答本群", "quote_source": True}
        replay.fake.receipt_ids[:] = [7550, 7551, 7552]
        await replay.receive(event(5250, 2261, "先在这个群答一句。", group=GROUP))
        await replay.step()
        await asyncio.wait_for(replay.assert_sent(5250), 2)
        await asyncio.gather(*channel._sending)

        started, finish = asyncio.Event(), asyncio.Event()
        original = replay.replyer.process

        async def delayed(request: OrganRequest) -> OrganResponse:
            started.set()
            await finish.wait()
            return await original(request)

        monkeypatch.setattr(replay.replyer, "process", delayed)
        active_group = GROUP + 1
        await replay.fake.send(
            replay.remote,
            **event(
                5251,
                2263,
                "",
                group=active_group,
                message=[
                    {"type": "at", "data": {"qq": str(ACCOUNT)}},
                    {"type": "text", "data": {"text": "等你答完这一句。"}},
                ],
            ),
        )
        await asyncio.wait_for(replay.accepted.get(), 2)
        heartbeat.beat()
        try:
            await asyncio.wait_for(started.wait(), 2)
            await replay.fake.send(
                replay.remote,
                **event(
                    5252,
                    2262,
                    "",
                    group=GROUP,
                    message=[
                        {"type": "reply", "data": {"id": "7550"}},
                        {"type": "text", "data": {"text": "回到刚才那句，还有呢？"}},
                    ],
                ),
            )
            await asyncio.wait_for(replay.accepted.get(), 2)
            heartbeat.beat()
            assert replay.fake.messages.empty()
        finally:
            finish.set()
        await heartbeat.wait_for_thought()
        heartbeat.beat()
        first = await asyncio.wait_for(replay.fake.messages.get(), 2)
        assert first["group_id"] == str(active_group)
        assert first["message"][0] == {"type": "reply", "data": {"id": "5251"}}
        await asyncio.gather(*channel._sending)
        await replay.step()
        await asyncio.wait_for(replay.assert_sent(5252), 2)
        assert replay.fake.messages.empty()
