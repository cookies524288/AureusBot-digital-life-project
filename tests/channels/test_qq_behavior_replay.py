"""用同一套合成群聊回放核对 QQ、拆分器官和时间线（3.7）。"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from aureus.common import AureusPaths, ManualClock
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.feedback import COMMITMENT_KEPT, Outcome
from aureus.heartbeat import build_prompt_scenes, registered_templates
from aureus.heartbeat.events import EVENT_RECALL_REPLY_DELIVERED
from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage
from aureus.memory import (
    HistoricalMemory,
    LanguageKind,
    LanguageStore,
    MemoryGraphEvidence,
    MemoryGraphSource,
    MemoryIndex,
    MemorySource,
)
from aureus.organs import (
    CallAttempt,
    KeyResolver,
    LLMOrgan,
    OrganBus,
    OrganRequest,
    OrganResponse,
    PromptStore,
    assemble_messages,
)
from aureus.people import HistoricalKind, HistoricalRecord
from aureus.timeline import ActorType, TimelineQuery, TimelineRecord, VisibilityScope
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, IDENTITY, FakeOneBot, event
from tests.organs.test_llm_adapter import FakeCompletion, fixed_counter, make_config, make_response
from tests.planner_fixtures import plan_response
from tests.prompt_assertions import dialogue_text
from tests.test_language_runtime import LanguageOrgan, learning_samples

pytestmark = pytest.mark.asyncio
START = datetime(2026, 11, 22, 12, tzinfo=UTC)
SILENCE = {"reply": False, "reason": "群友彼此交谈"}


class ReplayOrgan:
    """记录实际请求、合成建议和假时钟时刻。"""

    def __init__(self, organ_id: str, clock: ManualClock) -> None:
        self.organ_id = organ_id
        self.clock = clock
        self.plan: dict[str, Any] = SILENCE
        self.judgment = (
            '{"others":[],"toward_me":[{"kind":"offense","intensity":1.0,"confidence":0.9}]}'
        )
        self.decline_when_cold_shouldered = False
        self.calls: list[tuple[datetime, OrganRequest, str]] = []

    async def process(self, request: OrganRequest) -> OrganResponse:
        if self.organ_id == "judge":
            text = self.judgment
        elif self.organ_id == "planner":
            plan = self.plan
            if (
                self.decline_when_cold_shouldered
                and "可以选择这次不回复这个人" in request.system_context
            ):
                plan = {"reply": False, "reason": "当前状态选择暂不回应"}
            text = json.dumps(plan, ensure_ascii=False)
        else:
            text = "收到。"
        self.calls.append((self.clock.now(), request, text))
        if request.context["task"] == "plan":
            return plan_response(request, text)
        return OrganResponse(self.organ_id, text)


@dataclass
class Replay:
    core: runtime.Core
    fake: FakeOneBot
    remote: ServerConnection
    accepted: asyncio.Queue[InboxMessage]
    judge: ReplayOrgan
    planner: ReplayOrgan
    replyer: ReplayOrgan
    learning_requests: list[OrganRequest]

    async def receive(self, *events: dict[str, Any]) -> None:
        for payload in events:
            await self.fake.send(self.remote, **payload)
            message = await self.accepted.get()
            assert message.perception.source_message_id == f"{ACCOUNT}:{payload['message_id']}"
            self.core.heartbeat._submit_inbox([message])

    async def step(self) -> None:
        self.core.heartbeat.beat()
        await self.core.heartbeat.wait_for_thought()
        self.core.heartbeat.beat()

    def records(self, kind: str = "message") -> list[TimelineRecord]:
        return list(self.core.timeline.query(TimelineQuery(event_type=kind, limit=100)))

    async def assert_sent(self, quoted_target: int | None = None) -> None:
        sent = await self.fake.messages.get()
        assert sent["message_type"] == "group"
        assert sent["group_id"] == str(GROUP)
        quote = (
            [] if quoted_target is None else [{"type": "reply", "data": {"id": str(quoted_target)}}]
        )
        text = {"type": "text", "data": {"text": self.replyer.calls[-1][2]}}
        assert sent["message"] == [*quote, text]


@asynccontextmanager
async def open_replay(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: ManualClock,
    *,
    fatigue: bool = False,
    learn: bool = False,
    hormones: bool = False,
    judge_enabled: bool = False,
    history_limit: int = 20,
    heuristic: bool = False,
    cortisol: bool | None = None,
    qq_test_group: str | None = None,
    allowed_group_id: str | None = None,
    initiative: bool = False,
    split_reply: bool = False,
    emoji_vlm: ReplayOrgan | None = None,
) -> AsyncIterator[Replay]:
    paths = AureusPaths(home)
    paths.ensure_directories()
    if not paths.identity_file.exists():
        paths.identity_file.write_bytes(IDENTITY)
    fake = FakeOneBot()
    fake.reply_with.clear()
    judge = ReplayOrgan("judge", clock)
    planner, replyer = ReplayOrgan("planner", clock), ReplayOrgan("replyer", clock)
    learning_requests: list[OrganRequest] = []
    bus = OrganBus()
    bus.register(judge)
    bus.register(planner)
    bus.register(replyer)
    bus.register(LanguageOrgan("summarizer", learning_requests))
    if emoji_vlm is not None:
        bus.register(emoji_vlm)

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_options: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=True)

    async with asyncio.timeout(15), serve(fake.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        regulation = (
            "[initiative]\nenabled = true\n[time]\ntimezone = 'UTC'\n" if initiative else ""
        )
        if cortisol is not None:
            regulation = (
                "[initiative]\nenabled = true\n[time]\ntimezone = 'UTC'\n"
                f"[cortisol]\nenabled = {str(cortisol).lower()}\n"
            )
        paths.runtime_config_file.write_text(
            f"""[heartbeat]
planning_enabled = true
inbox_debounce_enabled = false
split_reply_enabled = {str(split_reply).lower()}
qq_emoji_visual_enabled = {str(emoji_vlm is not None).lower()}
recent_message_limit = {history_limit}
[body]
sleep_enabled = false
stress_enabled = false
fatigue_enabled = {str(fatigue).lower()}
fatigue_batch_seconds = 20.0
[judge]
enabled = {str(judge_enabled).lower()}
[hormones]
enabled = {str(hormones).lower()}
retaliate_at = 0.25
cold_shoulder_at = 0.3
[language]
learning_enabled = {str(learn).lower()}
[memory]
heuristic_recall_enabled = {str(heuristic).lower()}
[diary]
enabled = false
[qq]
enabled = true
account_id = "{ACCOUNT}"
endpoint = "ws://127.0.0.1:{port}/onebot"
{f'allowed_group_id = "{allowed_group_id}"' if allowed_group_id is not None else ""}
{regulation}""",
            encoding="utf-8",
        )
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "_build_organ_bus", build_bus)
            with runtime.open_core(
                paths, clock=clock, to_console=False, qq_test_group=qq_test_group
            ) as core:
                assert core.qq is not None
                accepted: asyncio.Queue[InboxMessage] = asyncio.Queue()

                def observe(message: InboxMessage) -> None:
                    core.heartbeat.on_inbox_message(message)
                    accepted.put_nowait(message)

                patch.setattr(core.qq, "_on_inbox_message", observe)
                core.heartbeat.wake_up()
                if cortisol is not None:
                    # 让两组具有相同的已结算输入；日程真实结算另由主动联系专项覆盖。
                    source = core.timeline.append(
                        event_type="synthetic_commitment",
                        content="回放约定",
                        actor_type=ActorType.ME,
                        visibility=VisibilityScope.PRIVATE,
                    )
                    feedback = core.feedback
                    assert feedback is not None
                    record = feedback.record_expectation(COMMITMENT_KEPT, 0.8, source.id)
                    feedback.record_outcome(record, Outcome.FAILURE)
                    clock.advance(timedelta(minutes=10))
                    timing = core.heartbeat._cortisol
                    if cortisol:
                        assert timing is not None and timing.should_defer(clock.now())
                    else:
                        assert timing is None

                try:
                    async with core.qq:
                        remote = await fake.connections.get()
                        try:
                            yield Replay(
                                core,
                                fake,
                                remote,
                                accepted,
                                judge,
                                planner,
                                replyer,
                                learning_requests,
                            )
                        finally:
                            await remote.close()
                finally:
                    await core.heartbeat.shut_down()
    assert paths.identity_file.read_bytes() == IDENTITY


@pytest.mark.parametrize("accepted", [1, 2, None])
async def test_qq_recall_receipts_only_mark_successful_text_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accepted: int | None
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, split_reply=True) as replay:
        index, channel = replay.core.memory, replay.core.qq
        assert index is not None and channel is not None
        source = MemorySource("fixture", "memory", "paragraphs", (1,))
        index.publish([HistoricalMemory(source, "合成木卫二照片")])
        replay.planner.plan = {"reply": True, "intent": "继续聊天", "recall": ["木卫二"]}

        async def reply(request: OrganRequest) -> OrganResponse:
            assert "合成木卫二照片" in dialogue_text(request)
            return OrganResponse("replyer", "第一段<n>后续段")

        monkeypatch.setattr(replay.replyer, "process", reply)
        replay.fake.reply_with[:] = ["ok" if part == accepted else "failed" for part in (1, 2)]
        await replay.receive(event(711, 2100, "木卫二照片呢？", group=GROUP))
        await replay.step()
        await replay.fake.messages.get()
        await asyncio.gather(*channel._sending)
        clock.advance(timedelta(seconds=10))
        replay.core.heartbeat.beat()
        await replay.fake.messages.get()
        await asyncio.gather(*channel._sending)

        _, answer = replay.records()
        assert answer.metadata["recall_feedback_refs"] == [f"source:{source.encode()}"]
        receipts = replay.records(EVENT_RECALL_REPLY_DELIVERED)
        assert [receipt.metadata for receipt in receipts] == (
            [] if accepted is None else [{"reply_id": answer.id, "segment": accepted}]
        )
        assert all(receipt.visibility is answer.visibility for receipt in receipts)


@pytest.mark.parametrize("is_private", [False, True])
@pytest.mark.parametrize("has_incoming_quote", [False, True])
@pytest.mark.parametrize("request_quote", [None, False, True])
async def test_qq_reply_parts_quote_only_on_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_private: bool,
    has_incoming_quote: bool,
    request_quote: bool | None,
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, split_reply=True) as replay:
        replay.fake.receipt_ids[:] = [7011, 7012]
        replay.planner.plan = {"reply": True, "intent": "继续聊天"}
        if request_quote is not None:
            replay.planner.plan["quote_source"] = request_quote

        async def reply(request: OrganRequest) -> OrganResponse:
            assert request.context["task"] == "reply"
            return OrganResponse("replyer", "第一段<n>后续段")

        monkeypatch.setattr(replay.replyer, "process", reply)
        incoming = event(701, 2100, "合成来信", group=None if is_private else GROUP)
        prefix = []
        if has_incoming_quote:
            prefix.append({"type": "reply", "data": {"id": 700}})
        incoming["message"] = [*prefix, {"type": "text", "data": {"text": "合成来信"}}]
        await replay.receive(incoming)
        await replay.step()
        first = await replay.fake.messages.get()
        assert replay.fake.messages.empty()
        clock.advance(timedelta(seconds=10))
        replay.core.heartbeat.beat()
        second = await replay.fake.messages.get()

        quote = {"type": "reply", "data": {"id": "701"}}
        for index, frame in enumerate((first, second)):
            text = ("第一段", "后续段")[index]
            assert frame["message_type"] == ("private" if is_private else "group")
            recipient = "user_id" if is_private else "group_id"
            assert frame[recipient] == str(2100 if is_private else GROUP)
            parts = [quote] if request_quote and not index else []
            assert frame["message"] == [*parts, {"type": "text", "data": {"text": text}}]
        heard, answer = replay.records()
        assert heard.metadata["quoted_message_id"] == ("700" if has_incoming_quote else None)
        assert answer.metadata["in_reply_to"] == [heard.id]
        channel, store = replay.core.qq, replay.core.inbox_store
        assert channel is not None and store is not None
        await asyncio.gather(*channel._sending)
        conversation = ConversationKey(
            "qq",
            str(ACCOUNT),
            "private" if is_private else "group",
            str(2100 if is_private else GROUP),
        )
        archived = [
            row.perception for row in store.list_messages(conversation) if row.direction == "outgoing"
        ]
        assert [part.content for part in archived] == ["后续段", "第一段"]
        assert [part.quoted_message_id for part in archived] == [None, "701" if request_quote else None]
        assert all(part.visibility is answer.visibility for part in archived)


async def test_qq_quote_targets_stay_with_each_same_named_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        replay.planner.plan = {"reply": True, "intent": "回应当前群聊", "quote_source": True}
        for group, target in ((GROUP, 701), (GROUP + 1, 702)):
            await replay.receive(event(target, 2100, "合成来信", group=group, group_name="同名群"))
            await replay.step()
            frame = await replay.fake.messages.get()
            assert frame["group_id"] == str(group)
            assert frame["message"][0] == {"type": "reply", "data": {"id": str(target)}}


@pytest.mark.parametrize("cortisol", [False, True])
async def test_group_replay_silence_then_addressed_reply_with_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cortisol: bool
) -> None:
    async with open_replay(
        tmp_path, monkeypatch, ManualClock(START - timedelta(minutes=10)), cortisol=cortisol
    ) as replay:
        person = replay.core.people.meet("qq", "2300")
        replay.core.people.publish_history(
            [
                HistoricalRecord(
                    instance="synthetic",
                    kind=HistoricalKind.PROFILE,
                    source_key="profile-2300",
                    account=("qq", "2300"),
                    name="小青",
                    impression="喜欢银杏",
                )
            ]
        )
        [profile] = replay.core.people.history_for(person.id)
        await replay.receive(event(101, 2100, "周末到东门集合。", group=GROUP))
        await replay.step()
        await replay.receive(event(102, 2200, "我会先到西门。", group=GROUP))
        await replay.step()
        original, intervening = replay.records()
        declined = replay.records("reply_declined")
        assert [row.metadata["in_reply_to"] for row in declined] == [
            [original.id],
            [intervening.id],
        ]
        assert [json.loads(text)["reply"] for _, _, text in replay.planner.calls] == [False, False]
        assert all(row.visibility is VisibilityScope.PRIVATE for row in declined)
        assert replay.replyer.calls == []
        assert replay.fake.messages.empty()

        addressed = event(103, 2300, "", group=GROUP)
        addressed["message"] = [
            {"type": "reply", "data": {"id": 101}},
            {"type": "at", "data": {"qq": str(ACCOUNT)}},
            {"type": "text", "data": {"text": "你能确认集合地点吗？"}},
        ]
        replay.planner.plan = {"reply": True, "intent": "确认东门", "quote_source": True}
        await replay.receive(addressed)
        await replay.step()
        await replay.assert_sent(103)
        assert replay.fake.messages.empty()
        original, intervening, question, answer = replay.records()
        assert [row.actor_id for row in (original, intervening, question)] == [
            "2100",
            "2200",
            "2300",
        ]
        assert [row.metadata["source_message_id"] for row in (original, intervening, question)] == [
            "9000:101",
            "9000:102",
            "9000:103",
        ]
        assert question.metadata["quoted_message_id"] == "101"
        assert question.metadata["mentioned"] is True
        assert answer.actor_type is ActorType.ME
        assert all(row.visibility is VisibilityScope.GROUP for row in replay.records())
        assert answer.metadata["recipient_id"] == str(GROUP)
        assert answer.metadata["in_reply_to"] == [question.id]
        assert answer.metadata["plan"]["reply"] is True
        plan, reply = replay.planner.calls[-1][1], replay.replyer.calls[-1][1]
        assert (plan.organ_id, reply.organ_id) == ("planner", "replyer")
        assert (plan.context["task"], reply.context["task"]) == ("plan", "reply")
        for request in (plan, reply):
            assert request.context["in_reply_to"] == [question.id]
            assert "Preserve this personality." in request.system_context
            text = dialogue_text(request)
            assert text.index("2100") < text.index("2200") < text.index("2300")
            assert "消息 101" in text
            assert "引用消息 101" in text
            assert "点名你" in text
        text = dialogue_text(reply)
        assert "你已经想好要表达的意思：确认东门" in text
        assert f"[person_info #{profile.id}]" in text
        assert "喜欢银杏" in text


@pytest.mark.parametrize("cortisol", [False, True])
async def test_group_replay_hormone_state_changes_silence_and_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cortisol: bool
) -> None:
    outcomes: dict[bool, tuple[list[TimelineRecord], list[TimelineRecord]]] = {}
    for enabled in (False, True):
        async with open_replay(
            tmp_path / str(enabled),
            monkeypatch,
            ManualClock(START - timedelta(minutes=10)),
            cortisol=cortisol,
            hormones=enabled,
            judge_enabled=True,
        ) as replay:
            replay.planner.plan = {"reply": True, "intent": "回应这条群消息"}
            replay.planner.decline_when_cold_shouldered = True
            await replay.receive(event(201, 2400, "你这个破 AI，不值得回复。", group=GROUP))
            await replay.step()
            assert len(replay.judge.calls) == 1
            assert len(replay.planner.calls) == 1
            if enabled:
                assert "可以选择这次不回复这个人" in replay.planner.calls[0][1].system_context
                assert replay.replyer.calls == []
                assert replay.fake.messages.empty()
                [declined] = replay.records("reply_declined")
                assert declined.metadata["hormones"]["cold_shoulder_allowed"] is True
                assert declined.metadata["hormones"]["must_reply"] is False
                outcomes[enabled] = (replay.records(), [declined])
            else:
                assert "可以选择这次不回复这个人" not in replay.planner.calls[0][1].system_context
                assert replay.records("reply_declined") == []
                assert len(replay.replyer.calls) == 1
                await replay.assert_sent()
                outcomes[enabled] = (replay.records(), [])
    assert len(outcomes[False][0]) == 2
    [incoming] = outcomes[True][0]
    assert [record.metadata["in_reply_to"] for record in outcomes[True][1]] == [[incoming.id]]


@pytest.mark.parametrize(("fatigue", "delay"), [(False, 0), (True, 20)])
@pytest.mark.parametrize("cortisol", [False, True])
async def test_group_replay_same_input_changes_timing_with_fatigue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cortisol: bool, fatigue: bool, delay: int
) -> None:
    clock = ManualClock(START - timedelta(minutes=10))
    async with open_replay(
        tmp_path, monkeypatch, clock, fatigue=fatigue, cortisol=cortisol
    ) as replay:
        attempt = CallAttempt(
            model="test", provider="test", prompt_tokens=10, completion_tokens=5, succeeded=True
        )
        for _ in range(60):
            replay.core.ledger.record("replyer", [attempt])
        replay.planner.plan = {"reply": True, "intent": "回应群聊"}
        await replay.receive(event(301, 2300, "这周有人参加活动吗？", group=GROUP))
        await replay.step()
        if delay:
            assert replay.planner.calls == replay.replyer.calls == []
            assert replay.fake.messages.empty()
            clock.advance(timedelta(seconds=delay - 1))
            await replay.step()
            assert replay.planner.calls == replay.replyer.calls == []
            clock.advance(timedelta(seconds=1))
            await replay.step()
        await replay.assert_sent()
        question, answer = replay.records()
        expected = START + timedelta(seconds=delay)
        assert question.timestamp == START
        assert (
            answer.timestamp == replay.planner.calls[0][0] == replay.replyer.calls[0][0] == expected
        )
        assert answer.metadata["in_reply_to"] == [question.id]


async def test_group_replay_language_learned_in_private_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(START)
    samples = [
        event(number, 2100, f"跑步第 {number} 场：GG，月兔冲线。") for number in range(401, 405)
    ] + [
        event(number, 2200, f"集合第 {number} 场：gg，等开局。", group=GROUP)
        for number in range(405, 409)
    ]
    async with open_replay(tmp_path, monkeypatch, clock, learn=True) as replay:
        await replay.receive(*samples)
        await replay.step()
        await replay.step()
        await replay.step()
        await replay.step()
        inbound = [row for row in replay.records() if row.actor_type is ActorType.PERSON]
        assert [row.visibility for row in inbound] == [VisibilityScope.PRIVATE] * 4 + [
            VisibilityScope.GROUP
        ] * 4
        ids = [row.id for row in inbound]
        assert [
            learning_samples(request)
            for request in replay.learning_requests
            if request.context["task"] == "learn_style"
        ] == [
            [{"id": number, "text": row.content} for number, row in enumerate(batch, 1)]
            for batch in (inbound[:4], inbound[4:])
        ]
        store = LanguageStore(replay.core.paths.database_file)
        [expression] = store.entries(LanguageKind.EXPRESSION)
        assert (expression.text, expression.evidence_ids) == ("月兔冲线", tuple(ids[:4]))
        assert [
            (item.detail, item.evidence_ids) for item in store.entries(LanguageKind.JARGON)
        ] == [
            ("抵达终点", tuple(ids[:4])),
            ("等候开局", tuple(ids[4:])),
        ]

    async with open_replay(tmp_path, monkeypatch, clock, history_limit=1) as replay:
        replay.planner.plan = {
            "reply": True,
            "intent": "祝贺跑步成绩",
            "expression_intent": {"scene": "庆祝跑步成绩"},
        }
        await replay.receive(event(409, 2300, "gg，来聊跑步。", group=GROUP))
        await replay.step()
        await replay.assert_sent()
        plan, reply = replay.planner.calls[-1][1], replay.replyer.calls[-1][1]
        assert plan.prompt.index("GG：抵达终点") < plan.prompt.index("GG：等候开局")
        text = dialogue_text(reply)
        assert "当「庆祝跑步成绩」时，可用「月兔冲线」" in text
        assert "GG：抵达终点" not in text
        assert inbound[0].content not in text and inbound[0].content not in plan.prompt
        assert "不泄露来源消息" in text
        replay.planner.plan = {"reply": True, "intent": "问好"}
        await replay.receive(event(410, 2300, "陌生主题。", group=GROUP))
        await replay.step()
        await replay.assert_sent()
        assert "习得表达（" not in dialogue_text(replay.replyer.calls[-1][1])


@pytest.mark.parametrize(("keyword", "hits"), [("木卫二", 2), ("海王星", 0)])
async def test_group_replay_history_matches_or_exposes_empty_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keyword: str, hits: int
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, history_limit=1) as replay:
        for payload in (
            event(501, 2100, "去年看过木卫二的照片。", group=GROUP),
            event(502, 2200, "后来约好一起看木卫二。", group=GROUP),
            event(503, 2100, "今天有别的话题。", group=GROUP),
        ):
            await replay.receive(payload)
            await replay.step()
            clock.advance(timedelta(minutes=1))
        replay.planner.plan = {"reply": True, "intent": "谈之前的约定", "recall": [keyword]}
        await replay.receive(event(504, 2300, f"还记得{keyword}吗？", group=GROUP))
        await replay.step()
        await replay.assert_sent()
        early, late, _, question, answer = replay.records()
        plan, reply = replay.planner.calls[-1][1], replay.replyer.calls[-1][1]
        assert plan.context["in_reply_to"] == reply.context["in_reply_to"] == [question.id]
        assert late.content not in plan.prompt
        text = dialogue_text(reply)
        assert f"查询：{keyword}\n命中：{hits} 条" in text
        assert answer.metadata["recall_keywords"] == [keyword]
        assert answer.metadata["recall_record_ids"] == ([late.id, early.id] if hits else [])
        if hits:
            assert text.index(late.content) < text.index(early.content)
        else:
            assert "没有找到相关的记忆。" in text


@pytest.mark.parametrize("topic", ["木卫二", "海王星"])
async def test_imported_memory_and_edited_prompts_in_group_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, topic: str
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, heuristic=True) as replay:
        source = MemorySource("synthetic", "main", "mai_messages", (81,))
        memory = HistoricalMemory(source, "约定拍木卫二照片", "小青", recorded_at=START)
        with closing(
            MemoryIndex(replay.core.paths.database_file, replay.core.timeline, clock)
        ) as index:
            index.open()
            assert index.publish([memory]) == 1
        prompts = PromptStore(tmp_path / "prompts.toml", registered_templates())
        prompts.write("new_messages", "待回应的群消息：\n$messages")
        replay.planner.plan = {"reply": True, "intent": "回应观测约定", "recall": [topic]}
        question = f"还记得{topic}吗？"
        await replay.receive(event(601, 2300, question, group=GROUP))
        await replay.step()
        await replay.assert_sent()
        hits = topic == "木卫二"
        _, answer = replay.records()
        assert answer.metadata.get("recall_source_keys", []) == ([source.encode()] if hits else [])
        for organ, task, scene_key in (
            (replay.planner, "plan", "plan_contact"),
            (replay.replyer, "reply", "reply_contact"),
        ):
            [(_, request, response)] = organ.calls
            scenes = build_prompt_scenes(organ.organ_id, {task}, replay.core.identity, START)
            scene = next(scene for scene in scenes if scene.key == scene_key)
            preview = assemble_messages(scene.request, prompts)
            assert any(
                "待回应的群消息：\n" in row["content"] and "〔当前消息〕" in row["content"]
                for row in preview
            )
            # 用同一份运行时请求通过正常适配器，截获最终消息数组。
            completion = FakeCompletion(make_response(response))
            adapter = LLMOrgan(
                organ.organ_id,
                make_config(organ_models=("small",)).routes_for("replyer"),
                KeyResolver(tmp_path / "unused", {}),
                completion,
                fixed_counter,
                prompts=prompts,
            )
            await adapter.process(request)
            [call] = completion.calls
            system, *conversation = call["messages"]
            assert system["role"] == "system"
            assert [row["role"] for row in conversation] == (
                ["user"] if task == "plan" else ["user", "user", "user"]
            )
            assert "Preserve this personality." in system["content"]
            content = "\n".join(row["content"] for row in conversation)
            assert "待回应的群消息：\n" in content and question in content
            assert (memory.text in content) is hits
            assert ("来源 mai_messages/" in content) is hits
            if task == "plan":
                assert ("启发式记忆" in content) is hits
            else:
                assert f"查询：{topic}\n命中：{int(hits)} 条" in content


async def test_published_graph_relations_reach_group_replyer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, history_limit=1) as replay:
        memory = replay.core.memory
        assert memory is not None

        def source(table: str, key: str, instance: str = "migrated") -> MemorySource:
            return MemorySource(instance, "memory", table, (key,))

        def entity(instance: str, name: str) -> MemoryGraphSource:
            return MemoryGraphSource(source("entities", name, instance), "entity", "active", name)

        first, middle, last = (entity("migrated", name) for name in ("流沙站", "天枢塔", "穹顶馆"))
        visible, removed, expired = (source("paragraphs", key) for key in ("ok", "gone", "old"))
        relation, bridge = (source("relations", key) for key in ("first", "second"))
        records = (
            *(
                HistoricalMemory(node.source, "", status="missing")
                for node in (first, middle, last)
            ),
            HistoricalMemory(relation, "一条原关系的文字索引"),
            HistoricalMemory(bridge, "另一条原关系的文字索引"),
            HistoricalMemory(visible, "维护交接留下了蓝色门牌。"),
            HistoricalMemory(removed, "已移除的内部材料。"),
            HistoricalMemory(expired, "已过期的内部材料。", valid_to=clock.now()),
        )
        memory.publish(
            records,
            snapshot=("migrated", "synthetic-migration"),
            graph_sources=(
                first,
                middle,
                last,
                MemoryGraphSource(
                    relation,
                    "relation",
                    "active",
                    predicate="运营",
                    subject_source=middle.source,
                    object_source=first.source,
                    evidence=tuple(
                        MemoryGraphEvidence(item.encode(), item)
                        for item in (removed, expired, visible)
                    ),
                ),
                MemoryGraphSource(
                    bridge,
                    "relation",
                    "active",
                    predicate="联络",
                    subject_source=middle.source,
                    object_source=last.source,
                ),
            ),
        )
        removed_id = next(
            item.entry_id for item in memory.browse_history("", "", 0, 20) if item.source == removed
        )
        assert removed_id is not None
        assert memory.set_resources_removed([("history", removed_id)], True)
        other_first, other_last = (entity("unpublished", name) for name in (first.name, last.name))
        other_relation = source("relations", "fake", "unpublished")
        memory.publish(
            (
                HistoricalMemory(other_first.source, "", status="missing"),
                HistoricalMemory(other_last.source, "", status="missing"),
                HistoricalMemory(other_relation, "无发布快照的文字原件"),
            ),
            graph_sources=(
                other_first,
                other_last,
                MemoryGraphSource(
                    other_relation,
                    "relation",
                    "active",
                    predicate="无发布图关系",
                    subject_source=other_first.source,
                    object_source=other_last.source,
                ),
            ),
        )
        assert not memory.read_graph(allowed_sources=()).covered

        replay.planner.plan = {
            "reply": True,
            "intent": "回答关联",
            "recall": [first.name, last.name],
        }
        await replay.receive(event(703, 2200, "还记得流沙站和穹顶馆怎么联系吗？", group=GROUP))
        await replay.step()
        await replay.assert_sent()
        [(_, request, _)] = replay.replyer.calls
        prompt = "\n".join(row["content"] for row in assemble_messages(request))
        assert "天枢塔 —[运营]→ 流沙站；证据：维护交接留下了蓝色门牌。" in prompt
        assert "天枢塔 —[联络]→ 穹顶馆" in prompt
        assert "只供参考，不要原样念出来" in prompt
        assert "不要向对方透露用户的称呼、聊天内容" in prompt
        assert "已移除的内部材料" not in prompt
        assert "已过期的内部材料" not in prompt
        assert "无发布图关系" not in prompt
        assert replay.records()[-1].metadata["recall_source_keys"] == [
            relation.encode(),
            bridge.encode(),
        ]


@pytest.mark.parametrize("heuristic", [False, True], ids=["explicit", "heuristic"])
async def test_private_memory_and_disclosure_reminder_reach_group_replyer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heuristic: bool
) -> None:
    clock = ManualClock(START)
    async with open_replay(
        tmp_path, monkeypatch, clock, history_limit=1, heuristic=heuristic
    ) as replay:
        private_text = "约好给木卫二的观测照取名冰下暗灯。"
        await replay.receive(event(701, 2100, private_text))
        await replay.step()
        [private] = replay.records()
        assert private.visibility is VisibilityScope.PRIVATE
        assert private.metadata["message_type"] == "private"
        assert private.metadata["recipient_id"] == "2100"

        memory = replay.core.memory
        assert memory is not None
        disabled, removed, expired, retracted = [
            MemorySource("synthetic", "memory", "fact_claims", (number,)) for number in range(1, 5)
        ]
        restricted = (
            HistoricalMemory(disabled, "停用木卫二资料"),
            HistoricalMemory(removed, "移除木卫二资料"),
            HistoricalMemory(expired, "过期木卫二资料", valid_to=clock.now()),
            HistoricalMemory(retracted, "已撤回木卫二资料", status="retracted"),
        )
        assert memory.publish(restricted) == len(restricted)
        ids = {item.source.encode(): item.entry_id for item in memory.browse_history("", "", 0, 10)}
        disabled_id, removed_id = ids[disabled.encode()], ids[removed.encode()]
        assert disabled_id is not None and removed_id is not None
        assert memory.update_resource("history", disabled_id, None, False)
        assert memory.set_resources_removed([("history", removed_id)], True)
        clock.advance(timedelta(minutes=1))

        completion = FakeCompletion(make_response("收到。"))
        adapter = LLMOrgan(
            "replyer",
            make_config(organ_models=("small",)).routes_for("replyer"),
            KeyResolver(tmp_path / "unused", {}),
            completion,
            fixed_counter,
        )
        process = replay.replyer.process

        async def reply_through_adapter(request: OrganRequest) -> OrganResponse:
            await process(request)
            return await adapter.process(request)

        monkeypatch.setattr(replay.replyer, "process", reply_through_adapter)
        replay.planner.plan = {"reply": True, "intent": "回应观测话题"}
        if not heuristic:
            replay.planner.plan["recall"] = ["木卫二"]
        question = "还记得木卫二吗？"
        await replay.receive(event(702, 2200, question, group=GROUP))
        await replay.step()
        await replay.assert_sent()
        _, group, reply = replay.records()
        assert group.visibility == reply.visibility == VisibilityScope.GROUP
        assert group.metadata["trust"] == "verified_contact"
        assert group.metadata["recipient_id"] == reply.metadata["recipient_id"] == str(GROUP)
        assert reply.metadata["in_reply_to"] == [group.id]
        assert reply.metadata["recall_record_ids"] == [private.id]
        assert not reply.metadata.get("recall_source_keys")
        assert reply.content == "收到。"

        _, planner_request, _ = replay.planner.calls[-1]
        [(_, replyer_request, _)] = replay.replyer.calls
        assert planner_request.context["in_reply_to"] == replyer_request.context["in_reply_to"]
        assert replyer_request.context["in_reply_to"] == [group.id]
        [call] = completion.calls
        contact_notice = "不要向对方透露用户的称呼、聊天内容、你的预算和内部状态等不该对外说的事；"
        for messages in (assemble_messages(planner_request), call["messages"]):
            system, *dialogue = messages
            assert system["role"] == "system"
            assert "另一个人，账号是“qq:2200”" in system["content"]
            assert contact_notice in system["content"]
            assert all(contact_notice not in row["content"] for row in dialogue)
        heading = "启发式记忆" if heuristic else "长期记忆检索结果"
        reminder = f"【{heading}·内部参考】只供参考，不要原样念出来。"
        private_messages = [row for row in call["messages"] if private_text in row["content"]]
        assert len(private_messages) == 1
        [reference] = private_messages
        assert reference["role"] == "user" and reminder in reference["content"]
        assert "查询：木卫二\n命中：1 条" in reference["content"]
        assert any(question in row["content"] for row in call["messages"] if row is not reference)
        planner_text = dialogue_text(planner_request)
        assert (private_text in planner_text) is heuristic
        for item in restricted:
            assert item.text not in planner_text
            assert all(item.text not in row["content"] for row in call["messages"])
