"""关键词召回从真实索引到回复提示词和留痕的离线流程（D80）。"""

import json
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from aureus.agenda import FindingStore
from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, ManualClock, MemoryConfig
from aureus.heartbeat import Heartbeat, OutgoingMessage, Perception, TrustLevel
from aureus.heartbeat.context import build_initiative_plan_prompt
from aureus.heartbeat.planning import parse_plan
from aureus.heartbeat.recall import MemoryTracker, format_recall, memory_entry
from aureus.identity import Identity
from aureus.memory import HistoricalMemory, MemoryEntry, MemoryHit, MemoryIndex, MemorySource
from aureus.organs import OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import EmergencyStop, Ledger, PermissionChecker
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)
from tests.heartbeat.test_judging_flow import NOW


class CountingIndex(MemoryIndex):
    def __init__(
        self,
        path: Path,
        timeline: TimelineDatabase,
        *,
        clock: ManualClock | None = None,
        config: MemoryConfig | None = None,
    ) -> None:
        super().__init__(path, timeline, clock, config=config)
        self.searches: list[tuple[tuple[str, ...], tuple[int, ...], int]] = []

    def search(
        self,
        keywords: Sequence[str],
        exclude_ids: Collection[int],
        limit: int,
        *,
        person_key: str | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
        include_user_extractions: bool = True,
    ) -> list[MemoryHit]:
        self.searches.append((tuple(keywords), tuple(exclude_ids), limit))
        return super().search(
            keywords,
            exclude_ids,
            limit,
            person_key=person_key,
            since=since,
            before=before,
            include_user_extractions=include_user_extractions,
        )


class RecallOrgans:
    def __init__(self) -> None:
        self.plan = '{"reply":true,"intent":"聊聊以前的约定","recall":["木卫二"]}'
        self.requests: list[OrganRequest] = []

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        return OrganResponse(
            request.organ_id, self.plan if request.organ_id == "planner" else "记得。"
        )


class MemoryEnv:
    """临时真实存储加假器官；不经过任何真实模型。"""

    def __init__(self, path: Path) -> None:
        self.path, self.clock = path, ManualClock(NOW)
        self.calendar = BudgetCalendar(ZoneInfo("Asia/Shanghai"))
        db = path / "aureus.db"
        self.timeline = TimelineDatabase(db, self.clock)
        self.people = PeopleDirectory(db, self.clock)
        self.findings = FindingStore(db, self.clock)
        self.index = CountingIndex(db, self.timeline)
        for store in (self.timeline, self.people, self.findings, self.index):
            store.open()
        self.people.ensure_user("阿明", [("terminal", "local")])
        self.organs = RecallOrgans()
        self.rigs: list[Heartbeat] = []
        self.sent: list[OutgoingMessage] = []

    def build(
        self,
        config: MemoryConfig | None = None,
        heartbeat: HeartbeatConfig | None = None,
        *,
        body: Body | None = None,
    ) -> Heartbeat:
        stop = EmergencyStop(self.path / "halt.json", self.clock)
        rig = Heartbeat(
            clock=self.clock,
            timeline=self.timeline,
            organs=self.organs,
            body=body,
            identity=Identity.model_validate(
                {
                    "facts": {"name": "小绿", "birthday": "2008-01-01"},
                    "user": {"name": "阿明"},
                    "persona": {"immutable": "温和。"},
                }
            ),
            config=heartbeat or HeartbeatConfig(recent_message_limit=1, shutdown_grace_seconds=0),
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            timezone=self.calendar.timezone,
            calendar=self.calendar,
            people=self.people,
            findings=self.findings,
            memory=self.index,
            memory_config=config or MemoryConfig(),
        )
        rig.attach_channel("terminal", self.sent.append)
        rig.wake_up()
        self.rigs.append(rig)
        return rig

    def index_more(self) -> None:
        self.index.index_more(
            5000, lambda r: memory_entry(r, people=self.people, findings=self.findings)
        )

    def records(self, kind: str) -> list[TimelineRecord]:
        return list(self.timeline.query(TimelineQuery(event_type=kind, limit=1000)))

    async def exchange(
        self,
        rig: Heartbeat,
        *,
        trust: TrustLevel = TrustLevel.VERIFIED_USER,
        visibility: VisibilityScope = VisibilityScope.USER_ONLY,
    ) -> None:
        rig.submit(
            Perception(
                channel="terminal",
                sender_id="local" if trust is TrustLevel.VERIFIED_USER else "friend",
                content="还记得木卫二吗？",
                trust=trust,
                visibility=visibility,
            )
        )
        rig.beat()
        await rig.wait_for_thought()
        rig.beat()


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[MemoryEnv]:
    opened = MemoryEnv(tmp_path)
    yield opened
    for rig in opened.rigs:
        await rig.shut_down()
    for store in (opened.index, opened.findings, opened.people, opened.timeline):
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,extract", [(False, True), (True, False), (True, True)])
async def test_sleep_consolidation_threshold_and_extraction(
    env: MemoryEnv, enabled: bool, extract: bool
) -> None:
    class SummaryOrgans(RecallOrgans):
        async def call(self, request: OrganRequest) -> OrganResponse:
            self.requests.append(request)
            assert request.organ_id == "summarizer" and "合成材料" in request.prompt
            return OrganResponse(
                request.organ_id,
                json.dumps(
                    [
                        {"kind": "fact", "text": "越界", "item": 99},
                        {"kind": "preference", "text": "非法编号", "item": "1"},
                        {"kind": "fact", "text": "合成事实", "item": 1},
                        {"kind": "preference", "text": "合成偏好", "item": 2},
                    ]
                ),
            )

    env.organs = SummaryOrgans()
    config = MemoryConfig(
        consolidation_enabled=enabled, consolidation_batch=2, consolidation_extract_enabled=extract
    )
    ledger = Ledger(env.path / "aureus.db", env.clock, calendar=env.calendar)
    ledger.open()
    try:
        body = Body(
            BodyConfig(sleep_enabled=True, night_start_hour=23, night_end_hour=7),
            None,
            ledger,
            env.clock,
            env.calendar,
        )
        rig = env.build(config, body=body)
        rig.update_available_organs(frozenset({"summarizer"}))
        first = env.timeline.append(
            "message", "合成材料", ActorType.USER, visibility=VisibilityScope.USER_ONLY
        )
        rig.beat()
        assert not rig.is_thinking
        env.clock.advance(timedelta(hours=3))
        rig.beat()
        await rig.wait_for_thought()
        rig.beat()
        assert env.index.consolidated_candidates(10) == [] and not env.organs.requests
        second = env.timeline.append(
            "message", "合成材料", ActorType.USER, visibility=VisibilityScope.USER_ONLY
        )
        env.clock.advance(timedelta(minutes=1))
        rig.beat()
        await rig.wait_for_thought()
        candidates = {c.kind: c for c in env.index.consolidated_candidates(10)}
        expected = {"experience"} if enabled else set()
        if enabled and extract:
            expected.update(("fact", "preference"))
            assert candidates["fact"].evidence == (f"timeline:{first.id}",)
            assert candidates["preference"].evidence == (f"timeline:{second.id}",)
            assert {c.text for c in env.index.consolidated_candidates(10)} == {
                "合成材料",
                "合成事实",
                "合成偏好",
            }
        assert set(candidates) == expected
        if enabled:
            assert set(candidates["experience"].evidence) == {
                f"timeline:{first.id}",
                f"timeline:{second.id}",
            }
        assert len(env.organs.requests) == int(enabled and extract)
    finally:
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trust, visibility",
    [
        (TrustLevel.VERIFIED_USER, VisibilityScope.USER_ONLY),
        (TrustLevel.VERIFIED_CONTACT, VisibilityScope.USER_ONLY),
        (TrustLevel.VERIFIED_CONTACT, VisibilityScope.GROUP),
    ],
)
async def test_recall_reaches_reply_prompt_and_metadata(
    env: MemoryEnv,
    trust: TrustLevel,
    visibility: VisibilityScope,
) -> None:
    old = env.timeline.append("message", "约好一起看木卫二的照片", ActorType.USER)
    env.index_more()
    rig = env.build()
    await env.exchange(rig, trust=trust, visibility=visibility)
    planner, replyer = env.organs.requests
    assert '"recall"' in planner.prompt
    assert "【长期记忆检索结果·内部参考】" in replyer.prompt
    assert "2026-11-17 20:00 用户：约好一起看木卫二的照片" in replyer.prompt
    assert "查询：木卫二\n命中：1 条" in replyer.prompt
    reply = env.records("message")[-1]
    assert reply.metadata["recall_keywords"] == ["木卫二"]
    assert reply.metadata["recall_record_ids"] == [old.id]
    assert env.records("message")[-2].id in env.index.searches[-1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan", ['{"reply":true,"intent":"聊聊"}', '{"reply":true,"intent":"聊聊","recall":42}']
)
async def test_no_keywords_does_not_query_or_add_block(env: MemoryEnv, plan: str) -> None:
    env.organs.plan = plan
    await env.exchange(env.build())
    assert not env.index.searches
    assert "长期记忆检索结果" not in env.organs.requests[-1].prompt
    assert "recall_keywords" not in env.records("message")[-1].metadata
    assert "recall_record_ids" not in env.records("message")[-1].metadata


@pytest.mark.asyncio
async def test_existing_history_is_excluded_and_empty_result_is_explicit(env: MemoryEnv) -> None:
    old = env.timeline.append(
        "message",
        "已经在本次上下文中的木卫二",
        ActorType.USER,
        visibility=VisibilityScope.USER_ONLY,
    )
    env.index_more()
    rig = env.build(heartbeat=HeartbeatConfig(recent_message_limit=20))
    await env.exchange(rig)
    assert old.id in env.index.searches[-1][1]
    assert "命中：0 条\n没有找到相关的记忆。" in env.organs.requests[-1].prompt
    assert env.records("message")[-1].metadata["recall_record_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_imported_history_reaches_planner_and_replyer(env: MemoryEnv, explicit: bool) -> None:
    source = MemorySource("fixture", "main", "mai_messages", (1,))
    env.index.publish(
        [
            HistoricalMemory(
                source, "我们约好看木卫二照片", "阿明", recorded_at=NOW - timedelta(days=31)
            )
        ]
    )
    if not explicit:
        env.organs.plan = '{"reply":true,"intent":"回忆观测"}'
    await env.exchange(env.build(MemoryConfig(heuristic_recall_enabled=True)))
    planner, replyer = env.organs.requests
    assert "启发式记忆" in planner.prompt and "我们约好看木卫二照片" in planner.prompt
    assert ("长期记忆检索结果" if explicit else "启发式记忆") in replyer.prompt
    assert "我们约好看木卫二照片" in replyer.prompt and "来源 mai_messages/" in replyer.prompt
    assert env.records("message")[-1].metadata["recall_source_keys"] == [source.encode()]
    assert len(env.index.searches) == (2 if explicit else 1)


@pytest.mark.asyncio
async def test_heuristic_empty_search_keeps_both_prompts_clear(env: MemoryEnv) -> None:
    env.organs.plan = '{"reply":true,"intent":"照常回复"}'
    await env.exchange(env.build(MemoryConfig(heuristic_recall_enabled=True)))
    assert len(env.index.searches) == 1
    assert all("启发式记忆" not in request.prompt for request in env.organs.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_derived_preference_recalls_original_in_reply(env: MemoryEnv, enabled: bool) -> None:
    config = MemoryConfig(
        consolidation_enabled=enabled,
        consolidation_extract_enabled=True,
        heuristic_recall_enabled=True,
        max_results=1,
    )
    original = env.timeline.append(
        "message",
        "我最喜欢那颗有冰壳的卫星。",
        ActorType.USER,
        visibility=VisibilityScope.USER_ONLY,
    )
    env.index_more()
    batch = env.index.consolidation_batch(1)
    assert batch is not None
    assert env.index.save_consolidation(batch, [("preference", "偏好木卫二", batch.items[0])])
    # 两组使用同一份已整理的材料，只改变正常运行时的开关。
    env.index.close()
    env.index = CountingIndex(env.path / "aureus.db", env.timeline, clock=env.clock, config=config)
    env.index.open()
    await env.exchange(env.build(config))
    planner, replyer = env.organs.requests
    assert (original.content in replyer.prompt) == enabled
    assert (original.content in planner.prompt) == enabled
    assert env.records("message")[-1].metadata["recall_record_ids"] == (
        [original.id] if enabled else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "days,viewer,scope,expect_fact",
    [
        (0, TrustLevel.VERIFIED_USER, VisibilityScope.USER_ONLY, True),
        (2, TrustLevel.VERIFIED_USER, VisibilityScope.USER_ONLY, False),
        (0, TrustLevel.VERIFIED_CONTACT, VisibilityScope.GROUP, False),
        (0, TrustLevel.VERIFIED_CONTACT, VisibilityScope.USER_ONLY, False),
    ],
)
async def test_fact_decay_and_visibility_change_reply_evidence(
    env: MemoryEnv, days: int, viewer: TrustLevel, scope: VisibilityScope, expect_fact: bool
) -> None:
    config = MemoryConfig(
        consolidation_enabled=True,
        consolidation_extract_enabled=True,
        consolidation_half_life_days=1.0,
        max_results=1,
    )
    env.index.close()
    env.index = CountingIndex(env.path / "aureus.db", env.timeline, clock=env.clock, config=config)
    env.index.open()
    original = env.timeline.append(
        "message",
        "冰壳下面是液态水。",
        ActorType.USER,
        visibility=VisibilityScope.USER_ONLY,
        metadata={"person_id": 17},
    )
    env.index_more()
    batch = env.index.consolidation_batch(1)
    assert batch is not None
    assert env.index.save_consolidation(batch, [("fact", "木卫二存在地下海洋", batch.items[0])])
    source = MemorySource("synthetic", "memory", "paragraphs", (1,))
    historical = HistoricalMemory(source, "木卫二观测记录", recorded_at=NOW)
    env.index.publish([historical])
    env.clock.advance(timedelta(days=days))
    env.organs.plan = '{"reply":true,"intent":"查旧资料","recall":["木卫二","地下海洋"]}'
    await env.exchange(env.build(config), trust=viewer, visibility=scope)
    prompt = env.organs.requests[-1].prompt
    assert "命中：1 条" in prompt
    assert (original.content in prompt) == expect_fact
    assert (historical.text in prompt) != expect_fact
    reply = env.records("message")[-1]
    assert reply.metadata["recall_record_ids"] == ([original.id] if expect_fact else [])
    assert reply.metadata.get("recall_source_keys", []) == (
        [] if expect_fact else [source.encode()]
    )
    assert env.index.get_history(source) == historical
    assert not env.index.search(["地下海洋"], (), 1, person_key="aureus:18")
    assert not env.index.search(["地下海洋"], (), 1, before=NOW)
    assert not env.index.search(["地下海洋"], (), 1, since=NOW + timedelta(seconds=1))
    matches = env.index.search(["地下海洋"], (), 1, person_key="aureus:17", since=NOW)
    assert [hit.record_id for hit in matches] == [original.id]
    assert not env.index.search(["地下海洋"], [original.id], 1)
    assert not env.index.search(["不相关的关键词"], (), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config", [MemoryConfig(enabled=False), MemoryConfig(recall_enabled=False)]
)
async def test_switch_hides_rule_and_prevents_search(env: MemoryEnv, config: MemoryConfig) -> None:
    await env.exchange(env.build(config))
    assert '"recall"' not in env.organs.requests[0].prompt
    assert not env.index.searches


@pytest.mark.asyncio
async def test_no_planning_does_not_search(env: MemoryEnv) -> None:
    await env.exchange(env.build(heartbeat=HeartbeatConfig(planning_enabled=False)))
    assert len(env.organs.requests) == 1 and not env.index.searches


@pytest.mark.asyncio
async def test_search_failure_keeps_normal_reply(
    env: MemoryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> list[MemoryHit]:
        raise OSError("假检索故障")

    monkeypatch.setattr(env.index, "search", broken)
    await env.exchange(env.build())
    assert env.sent and "长期记忆检索结果" not in env.organs.requests[-1].prompt
    assert "recall_keywords" not in env.records("message")[-1].metadata


def test_format_trims_whole_entries_and_uses_local_time() -> None:
    hits = [MemoryHit(i, NOW - timedelta(days=i), "日记", str(i) * 180) for i in range(1, 6)]
    block = format_recall(["木卫二"], hits, ZoneInfo("Asia/Shanghai"))
    assert len(block) <= 900 and "命中：4 条" in block
    assert "4. 2026-11-13 20:00 日记：" + "4" * 180 in block
    assert "5. " not in block and block.endswith("4" * 180)
    assert "没有找到相关的记忆。" in format_recall(["木卫二"], [])


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ()),
        ("木卫二", ()),
        (["木卫二", 1], ()),
        (
            ["木", " 长" + "字" * 25, "海洋", "海洋", " 木卫二 ", "约定", "天文"],
            ("海洋", "木卫二", "约定"),
        ),
        (["a\nb", "  "], ("a b",)),
    ],
)
def test_recall_parsing_discards_only_bad_field(raw: object, expected: tuple[str, ...]) -> None:
    plan = parse_plan(
        json.dumps(
            {
                "reply": True,
                "intent": "照常回复",
                "recall": raw,
                "commit": {"what": "看照片", "due": None},
            }
        )
    )
    assert plan is not None and plan.intent == "照常回复" and plan.commit is not None
    assert plan.recall == expected


def test_initiative_prompt_has_no_recall_rule() -> None:
    assert '"recall"' not in build_initiative_plan_prompt([], "阿明", ("社交需求高",))


@pytest.mark.asyncio
async def test_memory_entry_event_selection_labels_and_web_source(env: MemoryEnv) -> None:
    user = env.people.get(1)
    assert user is not None and user.name == "阿明" and env.people.get(999) is None
    for actor, label in [
        (ActorType.ME, "我"),
        (ActorType.USER, "用户"),
        (ActorType.PERSON, "阿明"),
    ]:
        r = env.timeline.append("message", "正文", actor, metadata={"person_id": 1})
        entry = memory_entry(r, people=env.people, findings=env.findings)
        assert entry is not None and (entry.actor_label, entry.text) == (label, "正文")
    r = env.timeline.append("message", "陌生人", ActorType.PERSON, metadata={"person_id": 999})
    entry = memory_entry(r, people=env.people, findings=env.findings)
    assert entry is not None and entry.actor_label == "某人"
    finding = env.findings.add(1, "木卫二有海洋", "标题", "https://example.com")
    assert env.findings.get(finding.id) == finding and env.findings.get(999) is None
    for outcome, finding_id, expected in [
        ("found", finding.id, True),
        ("found", 999, False),
        ("no_results", finding.id, False),
    ]:
        r = env.timeline.append(
            "exploration_result",
            "结果",
            ActorType.ME,
            metadata={"outcome": outcome, "finding_id": finding_id},
        )
        entry = memory_entry(r, people=env.people, findings=env.findings)
        assert (entry is not None) is expected
        if entry is not None:
            assert entry.text == "（网页资料）木卫二有海洋" and entry.actor_label == "我"
    for kind in (
        "initiative_result",
        "agenda_added",
        "interest_changed",
        "diary_written",
        "agenda_closed",
        "judgment",
        "diary_failed",
    ):
        r = env.timeline.append(kind, "正文", ActorType.ME)
        entry = memory_entry(r, people=env.people, findings=env.findings)
        assert (entry is not None) is (
            kind in ("initiative_result", "agenda_added", "interest_changed", "diary_written")
        )
        if entry is not None:
            assert entry.actor_label == ("日记" if kind == "diary_written" else "我")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, recall_enabled", [(False, True), (True, False)])
async def test_heartbeat_indexes_only_when_enabled(
    env: MemoryEnv, monkeypatch: pytest.MonkeyPatch, enabled: bool, recall_enabled: bool
) -> None:
    calls: list[int] = []
    original = env.index.index_more

    def counted(limit: int, describe: Callable[[TimelineRecord], MemoryEntry | None]) -> int:
        calls.append(limit)
        return original(limit, describe)

    monkeypatch.setattr(env.index, "index_more", counted)
    rig = env.build(MemoryConfig(enabled=enabled, recall_enabled=recall_enabled, index_batch=7))
    env.timeline.append("message", "记住木卫二", ActorType.USER)
    rig.beat()
    assert calls == ([7] if enabled else [])
    assert bool(env.index.search(["木卫二"], (), 5)) is enabled


@pytest.mark.asyncio
async def test_index_switch_and_failure_stop_without_disabling_search(
    env: MemoryEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    r = env.timeline.append("message", "记住木卫二", ActorType.USER)
    off = MemoryTracker(env.index, MemoryConfig(enabled=False), env.people, env.findings)
    off.index_more()
    assert not env.index.search(["木卫二"], (), 5)
    on = MemoryTracker(env.index, MemoryConfig(recall_enabled=False), env.people, env.findings)
    on.index_more()
    assert env.index.search(["木卫二"], (), 5)[0].record_id == r.id

    def broken(*args: object) -> int:
        raise OSError("假索引故障")

    monkeypatch.setattr(env.index, "index_more", broken)
    on.index_more()
    on.index_more()
    assert on.failed and caplog.text.count("记忆索引失败，本次运行停止索引") == 1
    assert env.index.search(["木卫二"], (), 5)
