"""关键词召回从真实索引到回复提示词和留痕的离线流程（D80）。"""

import json
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.agenda import FindingStore
from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, HeartbeatConfig, ManualClock, MemoryConfig
from aureus.heartbeat import Heartbeat, OutgoingMessage, Perception, TrustLevel
from aureus.heartbeat.context import build_initiative_plan_prompt
from aureus.heartbeat.events import EVENT_RECALL_REPLY_DELIVERED
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
from aureus.web.recall_feedback_api import register_recall_feedback_routes
from aureus.web.resource_api import register_resource_routes
from tests.heartbeat.test_judging_flow import NOW
from tests.prompt_assertions import dialogue_text


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
        person_key: str | tuple[str, ...] | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
        include_user_extractions: bool = True,
        summary_scope: tuple[str, str] | None = None,
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
            summary_scope=summary_scope,
        )


class RecallOrgans:
    def __init__(self) -> None:
        self.plan = '{"reply":true,"intent":"聊聊以前的约定","recall":["木卫二"]}'
        self.requests: list[OrganRequest] = []

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        from tests.planner_fixtures import plan_response

        if request.context.get("task") == "plan":
            return plan_response(request, self.plan)
        return OrganResponse(request.organ_id, "记得。")


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
        content: str = "还记得木卫二吗？",
        trust: TrustLevel = TrustLevel.VERIFIED_USER,
        visibility: VisibilityScope = VisibilityScope.USER_ONLY,
    ) -> None:
        rig.submit(
            Perception(
                channel="terminal",
                sender_id="local" if trust is TrustLevel.VERIFIED_USER else "friend",
                content=content,
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
async def test_sleep_consolidation_keeps_messages_out_of_legacy_sorting(
    env: MemoryEnv, enabled: bool, extract: bool
) -> None:
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
        env.timeline.append(
            "message", "合成材料", ActorType.USER, visibility=VisibilityScope.USER_ONLY
        )
        rig.beat()
        assert not rig.is_thinking
        env.clock.advance(timedelta(hours=3))
        rig.beat()
        await rig.wait_for_thought()
        rig.beat()
        assert env.index.consolidated_candidates(10) == [] and not env.organs.requests
        env.timeline.append(
            "message", "合成材料", ActorType.USER, visibility=VisibilityScope.USER_ONLY
        )
        env.clock.advance(timedelta(minutes=1))
        rig.beat()
        await rig.wait_for_thought()
        assert env.index.consolidated_candidates(10) == [] and not env.organs.requests
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
    planner, continued, replyer = env.organs.requests
    assert "query_memory" in {tool.name for tool in planner.tools}
    assert continued.prompt == planner.prompt
    [exchange] = continued.tool_history
    assert exchange.results[0].call_id == exchange.calls[0].call_id
    assert "命中：1 条" in exchange.results[0].content
    text = dialogue_text(replyer)
    assert "【长期记忆检索结果·内部参考】" in text
    assert "2026-11-17 20:00 用户：约好一起看木卫二的照片" in text
    assert "查询：木卫二\n命中：1 条" in text
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
    assert "长期记忆检索结果" not in dialogue_text(env.organs.requests[-1])
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
    assert "命中：0 条\n没有找到相关的记忆。" in dialogue_text(env.organs.requests[-1])
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
    planner, *_, replyer = env.organs.requests
    assert "启发式记忆" in planner.prompt and "我们约好看木卫二照片" in planner.prompt
    text = dialogue_text(replyer)
    assert ("长期记忆检索结果" if explicit else "启发式记忆") in text
    assert "我们约好看木卫二照片" in text and "来源 mai_messages/" in text
    assert env.records("message")[-1].metadata["recall_source_keys"] == [source.encode()]
    assert len(env.index.searches) == (2 if explicit else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_reply_metadata_only_records_hits_that_fit_the_prompt(
    env: MemoryEnv, explicit: bool
) -> None:
    local = {}
    for number in range(3):
        marker = f"本地木卫二{number}号"
        local[marker] = env.timeline.append("message", marker + "甲" * 170, ActorType.USER).id
    history = {
        f"历史木卫二{number}号": MemorySource("fixture", "main", "mai_messages", (number,))
        for number in range(3)
    }
    env.index.publish(
        HistoricalMemory(source, marker + "乙" * 170, recorded_at=NOW + timedelta(days=1))
        for marker, source in history.items()
    )
    env.index_more()
    if not explicit:
        env.organs.plan = '{"reply":true,"intent":"回忆观测"}'
    await env.exchange(env.build(MemoryConfig(max_results=8, heuristic_recall_enabled=True)))
    prompt = dialogue_text(env.organs.requests[-1])
    reply = env.records("message")[-1]
    metadata = reply.metadata
    shown_ids = [record_id for marker, record_id in local.items() if marker in prompt]
    shown_keys = [source.encode() for marker, source in history.items() if marker in prompt]
    assert 0 < len(shown_ids) + len(shown_keys) < len(local) + len(history)
    assert sorted(metadata["recall_record_ids"]) == sorted(shown_ids)
    assert sorted(metadata.get("recall_source_keys", [])) == sorted(shown_keys)
    shown = {f"timeline:{record_id}" for record_id in shown_ids} | {
        f"source:{source}" for source in shown_keys
    }
    all_refs = {f"timeline:{record_id}" for record_id in local.values()} | {
        f"source:{source.encode()}" for source in history.values()
    }
    [receipt] = env.records(EVENT_RECALL_REPLY_DELIVERED)
    assert receipt.metadata == {"reply_id": reply.id, "segment": 1}
    assert set(metadata["recall_feedback_refs"]) == shown

    async def operator() -> str:
        return "owner"

    app = FastAPI()
    register_recall_feedback_routes(
        app, env.timeline, operator, lambda _request: True, enabled=True
    )
    with TestClient(app) as client:
        path = f"/api/timeline/{reply.id}/recall-feedback"
        assert {item["key"] for item in client.get(path).json()} == {
            sha256(ref.encode()).hexdigest() for ref in shown
        }
        for ref in all_refs - shown:
            assert (
                client.patch(
                    path,
                    json={"evidence_key": sha256(ref.encode()).hexdigest(), "rating": "useful"},
                ).status_code
                == 404
            )


@pytest.mark.asyncio
async def test_batch_removal_suppresses_real_recall_without_changing_original_states(
    env: MemoryEnv,
) -> None:
    local = env.timeline.append("message", "本地木卫二相片", ActorType.USER)
    source = MemorySource("fixture", "main", "mai_messages", (1,))
    disabled = MemorySource("fixture", "main", "mai_messages", (2,))
    expired = MemorySource("fixture", "memory", "fact_claims", (3,))
    env.index.publish(
        [
            HistoricalMemory(source, "迁入木卫二照片"),
            HistoricalMemory(disabled, "停用木卫二文字"),
            HistoricalMemory(expired, "过期木卫二说明", valid_to=NOW - timedelta(days=1)),
        ]
    )
    env.index_more()

    async def require_session(request: Request) -> str:
        if request.cookies.get("session") != "owner":
            raise HTTPException(401)
        return "owner"

    app = FastAPI()
    register_resource_routes(
        app,
        require_session,
        None,
        env.index,
        same_origin=lambda request: request.headers.get("origin") == "http://testserver",
    )
    rig = env.build()
    await env.exchange(rig)
    prompt = dialogue_text(env.organs.requests[-1])
    assert "本地木卫二相片" in prompt and "迁入木卫二照片" in prompt
    endpoint = "/api/resources/memory"
    headers = {"origin": "http://testserver"}
    with TestClient(app) as client:
        assert (
            client.patch(endpoint + "/batch", json={"removed": True, "targets": []}).status_code
            == 401
        )
        client.cookies.set("session", "owner")
        params = {"kind": "all", "q": "木卫二"}
        original = client.get(endpoint, params=params).json()
        entries = {
            item["text"]: {"kind": item["kind"], "entry_id": item["entry_id"]}
            for item in original["items"]
        }
        selected = ("本地木卫二相片", "迁入木卫二照片", "停用木卫二文字", "过期木卫二说明")
        targets = [entries[text] for text in selected]
        assert (
            entries["本地木卫二相片"]["entry_id"]
            == entries["迁入木卫二照片"]["entry_id"]
            == local.id
        )
        assert client.patch(
            endpoint, json={**entries["停用木卫二文字"], "is_enabled": False}
        ).is_success
        body = {"removed": True, "targets": targets}
        assert client.patch(endpoint + "/batch", json=body).status_code == 403
        invalid = {**body, "targets": [*targets, {"kind": "history", "entry_id": 9999}]}
        assert client.patch(endpoint + "/batch", json=invalid, headers=headers).status_code == 409
        assert client.get(endpoint, params=params).json()["total"] == original["total"]
        assert client.patch(endpoint + "/batch", json=body, headers=headers).json() == {
            "changed": 4
        }
        remaining = client.get(endpoint, params=params).json()
        assert remaining["total"] == original["total"] - 4
        assert {item["text"] for item in remaining["items"]} == entries.keys() - set(selected)
        recycle = client.get(endpoint, params={**params, "removed": True}).json()
        assert recycle["total"] == 4
        assert all(not item["is_recallable"] for item in recycle["items"])
        await env.exchange(rig)
        prompt = dialogue_text(env.organs.requests[-1])
        assert "本地木卫二相片" not in prompt
        assert "迁入木卫二照片" not in prompt
        assert env.timeline.get(local.id) == local and env.index.get_history(source) is not None
        restored = {**body, "removed": False}
        assert client.patch(endpoint + "/batch", json=restored, headers=headers).json() == {
            "changed": 4
        }
        assert client.get(endpoint, params={**params, "removed": True}).json()["total"] == 0
    await env.exchange(rig)
    prompt = dialogue_text(env.organs.requests[-1])
    assert "本地木卫二相片" in prompt and "迁入木卫二照片" in prompt
    assert "停用木卫二文字" not in prompt and "过期木卫二说明" not in prompt


@pytest.mark.asyncio
async def test_heuristic_empty_search_keeps_both_prompts_clear(env: MemoryEnv) -> None:
    env.organs.plan = '{"reply":true,"intent":"照常回复"}'
    await env.exchange(env.build(MemoryConfig(heuristic_recall_enabled=True)))
    assert len(env.index.searches) == 1
    assert all("启发式记忆" not in dialogue_text(request) for request in env.organs.requests)


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
    planner, continued, replyer = env.organs.requests
    assert (original.content in dialogue_text(replyer)) == enabled
    assert (original.content in planner.prompt) == enabled
    assert (original.content in continued.tool_history[0].results[0].content) == enabled
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
    prompt = dialogue_text(env.organs.requests[-1])
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
    assert "query_memory" not in {tool.name for tool in env.organs.requests[0].tools}
    assert not env.index.searches


@pytest.mark.asyncio
async def test_no_recall_request_does_not_search(env: MemoryEnv) -> None:
    env.organs.plan = '{"reply":true,"intent":"照常回复"}'
    await env.exchange(
        env.build(MemoryConfig(heuristic_recall_enabled=True)), content="今天阳光很好。"
    )
    assert len(env.organs.requests) == 2 and not env.index.searches


@pytest.mark.asyncio
async def test_legacy_planning_switch_does_not_skip_recall(env: MemoryEnv) -> None:
    await env.exchange(env.build(heartbeat=HeartbeatConfig(planning_enabled=False)))
    assert len(env.organs.requests) == 3
    assert [keywords for keywords, _, _ in env.index.searches] == [("木卫二",)]


@pytest.mark.asyncio
async def test_search_failure_never_authorizes_reply(
    env: MemoryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> list[MemoryHit]:
        raise OSError("假检索故障")

    monkeypatch.setattr(env.index, "search", broken)
    await env.exchange(env.build())
    assert not env.sent and len(env.organs.requests) == 1
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
