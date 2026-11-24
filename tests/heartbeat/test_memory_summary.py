"""用假时钟和收件箱验证总结协议、证据边界及原子提交。"""

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aureus.common import ManualClock, MemoryConfig
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.memory_consolidation import MemoryConsolidator, summary_material
from aureus.heartbeat.memory_summary_source import PlatformSummarySource, summary_scope_for
from aureus.heartbeat.perception import MediaReference, MessageRoute, Perception, TrustLevel
from aureus.memory import (
    HistoricalMemory,
    LifecycleAction,
    LifecycleConflictError,
    MemoryIndex,
    MemorySource,
    SummaryScope,
    SummaryWindow,
    parse_delta,
)
from aureus.organs import ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.timeline import TimelineDatabase, VisibilityScope
from aureus.web.memory_lifecycle_api import register_memory_lifecycle_routes
from aureus.web.resource_api import register_resource_routes

_EMPTY = '{"summary":"","entities":[],"relations":[],"facts":[]}'
_SCOPE = VisibilityScope.GROUP
_PREFERENCE = ModelPreference.CHEAPEST


@dataclass
class Rig:
    path: Path
    clock: ManualClock
    inbox: InboxStore
    index: MemoryIndex
    source: PlatformSummarySource
    runner: MemoryConsolidator
    key: ConversationKey

    @property
    def scope(self) -> SummaryScope:
        return self.key.language_scope_key(), _SCOPE

    def add(
        self,
        message_id: str,
        content: str,
        *,
        sender: str = "200",
        name: str = "阿青",
        account: str = "100",
        kind: str = "group",
        recipient: str = "300",
        visibility: VisibilityScope = _SCOPE,
        image: bool = False,
        outgoing: bool = False,
        quote: str | None = None,
    ) -> None:
        perception = Perception(
            "qq",
            sender,
            content,
            visibility,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute(account, kind, recipient, message_id),
            quoted_message_id=quote,
            attachments=(MediaReference("image", "asset"),) if image else (),
        )
        if outgoing:
            self.inbox.add_delivered(perception, sender_name=name)
        else:
            assert self.inbox.add(perception, sender_name=name, conversation_name="同名会话")

    def window(self) -> SummaryWindow:
        window = self.source.next_window(self.runner.config.consolidation_batch)
        assert window is not None
        return window


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    path = tmp_path / "isolated.db"
    clock = ManualClock(datetime(2026, 11, 24, tzinfo=UTC))
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    inbox = InboxStore(path, clock)
    config = MemoryConfig(
        consolidation_enabled=True, consolidation_extract_enabled=True, consolidation_batch=2
    )
    index = MemoryIndex(path, timeline, clock, config=config)
    index.open()
    source = PlatformSummarySource(inbox, index)
    key = ConversationKey("qq", "100", "group", "300")
    source.allows = lambda candidate: candidate == key
    index.summary_evidence = source.check_evidence
    runner = MemoryConsolidator(index, config)
    runner.source, runner.has_organ = source, True
    try:
        yield Rig(path, clock, inbox, index, source, runner, key)
    finally:
        index.close()
        timeline.close()


def _answer(
    value: str,
    excerpt: str,
    *,
    message: str = "m1",
    sender: str = "200",
    image: tuple[str, str] | None = None,
    replaces: int | None = None,
    subject: str = "阿青",
) -> str:
    text = f"阿青：居住地点是{value}。"
    return json.dumps(
        {
            "summary": text,
            "entities": ["阿青", value],
            "relations": [
                {
                    "subject": subject,
                    "predicate": "居住地点",
                    "object": value,
                }
            ],
            "facts": [
                {
                    "fact_id": "f1",
                    "text": text,
                    "predicate": "居住地点",
                    "value": value,
                    "source": {"message_id": message, "sender_id": sender, "excerpt": excerpt},
                    "image_evidence": (
                        [{"message_id": image[0], "component_path": image[1]}] if image else []
                    ),
                    "replaces": replaces,
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_only_explicit_empty_advances_and_cancel_does_not_consume_window(rig: Rig) -> None:
    rig.add("m1", "我住在晴桂巷。")
    rig.add("m2", "合成上下文")
    attempts = iter([OrganCallError("summarizer", ["离线失败"]), "[]", "{", _EMPTY])
    entered = asyncio.Event()

    async def interrupted(request: OrganRequest) -> OrganResponse:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("等待会被取消")

    task = asyncio.create_task(rig.runner.run(interrupted, _PREFERENCE))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def answer(request: OrganRequest) -> OrganResponse:
        assert (
            request.organ_id == "summarizer" and request.context["task"] == "memory_consolidation"
        )
        value = next(attempts)
        if isinstance(value, OrganCallError):
            raise value
        return OrganResponse(request.organ_id, value)

    for _ in range(3):
        assert not (await rig.runner.run(answer, _PREFERENCE)).completed
        assert rig.index.summary_reference(rig.scope) == (0, (), ())
    assert (await rig.runner.run(answer, _PREFERENCE)).completed
    assert rig.index.summary_reference(rig.scope) == (2, (), ())


@pytest.mark.asyncio
async def test_same_window_once_and_image_not_interchangeable(rig: Rig) -> None:
    rig.add("outside", "我住在未授权的地点。", account="999", image=True)
    rig.add("m1", "我目前住在晴桂巷。", image=True)
    rig.add("m2", "我是阿青青。", sender="201", name="阿青青", image=True)
    rig.add("narrower", "我住在私下地点。", visibility=VisibilityScope.USER_ONLY)
    window = rig.window()
    materials = json.loads(summary_material(window, "模拟助手"))
    assert [entry["message_id"] for entry in materials["messages"]] == ["m1", "m2"]
    assert materials["image_evidence_catalog"] == [
        {"message_id": "m1", "component_path": "0"},
        {"message_id": "m2", "component_path": "0"},
    ]
    for image in [("m2", "0"), ("outside", "0"), ("m1", "1")]:
        with pytest.raises(ValueError):
            parse_delta(_answer("晴桂巷", "我目前住在晴桂巷。", image=image), window)
    with pytest.raises(ValueError):
        parse_delta(_answer("晴桂巷", "我目前住在晴桂巷。", subject="未出现的人"), window)
    delta = parse_delta(_answer("晴桂巷", "我目前住在晴桂巷。", image=("m1", "0")), window)

    async def commit() -> bool:
        return await asyncio.to_thread(
            rig.index.commit_summary, window, delta, rig.source.check_window
        )

    assert sorted(await asyncio.gather(commit(), commit())) == [False, True]
    assert not await commit()
    assert rig.index.search(["晴桂巷"], (), 4) == []
    hits = rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope)
    assert len(hits) == 1 and hits[0].text == "阿青：居住地点是晴桂巷。"
    assert [(item.kind, item.reference) for item in hits[0].evidence] == [
        ("platform_message", "m1"),
        ("platform_image", "0"),
    ]
    assert rig.index.search(["我目前住在晴桂巷"], (), 4, summary_scope=rig.scope) == []
    with sqlite3.connect(rig.path) as connection:
        assert connection.execute("SELECT count(*) FROM memory_summary_deltas").fetchone() == (1,)
        connection.execute(
            "DELETE FROM inbox_messages WHERE message_id IN ('m1', 'm2', 'narrower')"
        )
    assert rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope) == []
    assert rig.index.summary_reference(rig.scope) == (3, (), ())
    rig.add("m3", "我目前住在晴桂巷。")
    rig.add("m4", "其他合成上下文")
    assert rig.window().messages[0].sequence == 5


def test_authorized_cross_scope_recall_keeps_platform_text_private(rig: Rig) -> None:
    private = ConversationKey("qq", "100", "private", "200")
    rig.source.allows = lambda candidate: candidate in (rig.key, private)
    for identifier, content in (("m1", "我住在晴桂巷。"), ("m2", "确认住在晴桂巷。")):
        rig.add(
            identifier,
            content,
            kind="private",
            recipient="200",
            visibility=VisibilityScope.USER_ONLY,
        )
    window = rig.window()
    assert window.scope == (private.language_scope_key(), VisibilityScope.USER_ONLY)
    assert (
        summary_scope_for(
            "qq",
            "200",
            TrustLevel.VERIFIED_CONTACT,
            VisibilityScope.PRIVATE,
            private.language_scope_key(),
        )
        is None
    )
    delta = parse_delta(_answer("晴桂巷", "我住在晴桂巷。"), window)
    assert rig.index.commit_summary(window, delta, rig.source.check_window)
    rig.index.summary_scope_allowed = rig.source.allows_scope
    hits = rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope)
    assert len(hits) == 1
    assert hits[0].text == "阿青：居住地点是晴桂巷。"
    assert "我住在晴桂巷" not in hits[0].text
    assert hits[0].source is not None

    async def require_ready() -> str:
        return "synthetic-session"

    app = FastAPI()
    visible = True
    register_resource_routes(
        app, require_ready, None, rig.index, can_show_summary=lambda scope: visible
    )
    endpoint = "/api/resources/memory"
    detail = endpoint + "/facts/detail"
    location = {"source": hits[0].source.encode()}
    with TestClient(app) as client:
        [fact] = client.get(endpoint, params={"category": "fact_claims"}).json()["items"]
        assert fact["source"]["table"] == "summary_facts"
        response = client.get(detail, params=location)
        assert response.json()["text"] == hits[0].text
        assert response.json()["evidence"][0]["reference"] == "m1"
        assert "我住在晴桂巷。" not in response.text
        for invalid in (
            '["aureus","memory","summary_facts",[999999999999999999999]]',
            '["aureus","memory","summary_facts",null]',
            '["aureus","memory","summary_facts",[true]]',
        ):
            assert client.get(detail, params={"source": invalid}).status_code == 404
        visible = False
        assert client.get(detail, params=location).status_code == 404
        assert client.get(endpoint, params={"category": "fact_claims"}).json()["counts"] == {}
        assert rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope) == hits
        visible = True
        rig.source.allows = lambda candidate: candidate == private
        assert not rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope)
        assert not rig.index.recheck_summary_hits(hits, rig.scope)
        rig.source.allows = lambda candidate: candidate == rig.key
        assert rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope) == []
        assert not rig.index.recheck_summary_hits(hits, rig.scope)
        assert client.get(endpoint, params={"category": "fact_claims"}).json()["counts"] == {}
        assert client.get(detail, params=location).status_code == 404
        rig.source.allows = lambda candidate: candidate in (rig.key, private)
        check = rig.index.summary_evidence
        assert check is not None

        def revoke_during_catalog(*args: object) -> bool:
            nonlocal visible
            visible = False
            return True

        rig.index.summary_evidence = revoke_during_catalog
        changed = client.get(endpoint, params={"category": "fact_claims", "offset": 20})
        assert changed.status_code == 409 and "counts" not in changed.json()
        rig.index.summary_evidence = None
        assert not rig.index.recheck_summary_hits(hits, rig.scope)
        rig.index.summary_evidence = check
        visible = True
        with sqlite3.connect(rig.path) as connection:
            connection.execute("DELETE FROM inbox_messages WHERE message_id = 'm1'")
        unavailable = client.get(detail, params=location).json()
        assert unavailable["status"] == "missing"
        assert unavailable["text"] == "" and unavailable["evidence"] == []


@pytest.mark.parametrize(
    ("excerpt", "outgoing", "quote"),
    [
        ("我目前住在晴桂巷。", True, None),
        ("> 我目前住在晴桂巷。", False, "old"),
        ("我听说他现在住在晴桂巷。", False, None),
        ("我可能住在晴桂巷。", False, None),
        ("我给个示例：晴桂巷。", False, None),
        ("我只是玩笑，晴桂巷。", False, None),
    ],
)
def test_context_cannot_be_self_evidence(
    rig: Rig,
    excerpt: str,
    outgoing: bool,
    quote: str | None,
) -> None:
    rig.add("m1", excerpt, outgoing=outgoing, quote=quote, image=True)
    rig.add("m2", "我确认另一件事情。")
    window = rig.window()
    assert not window.messages[0].can_be_evidence
    with pytest.raises(ValueError):
        parse_delta(_answer("晴桂巷", excerpt, image=("m1", "0")), window)
    assert rig.index.summary_reference(rig.scope) == (0, (), ())


def test_correction_retains_history_and_rechecks_route_under_transaction(rig: Rig) -> None:
    original = "我一直住在晴桂巷。"
    rig.add("m1", original)
    rig.add("m2", "第二条原件")
    first = rig.window()
    delta = parse_delta(_answer("晴桂巷", original), first)
    assert rig.index.commit_summary(first, delta, rig.source.check_window)
    corrected = "我更正一下，现在住在栖松巷。"
    rig.add("m3", corrected)
    rig.add("m4", original)
    window = rig.window()
    assert window.previous_summaries == ("阿青：居住地点是晴桂巷。",)
    old = window.previous_facts[0]
    with pytest.raises(ValueError):
        parse_delta(_answer("晴桂巷", original, message="m4"), window)
    with pytest.raises(ValueError):
        parse_delta(_answer("栖松巷", corrected, message="m3", replaces=9999), window)
    with pytest.raises(ValueError):
        parse_delta(_answer("栖松巷", corrected, message="m3"), window)
    delta = parse_delta(_answer("栖松巷", corrected, message="m3", replaces=old.identifier), window)
    rig.source.allows = lambda _: False
    assert not rig.index.commit_summary(window, delta, rig.source.check_window)
    assert rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope) == []
    rig.source.allows = lambda key: key == rig.key
    assert rig.index.commit_summary(window, delta, rig.source.check_window)
    assert len(rig.index.search(["栖松巷"], (), 4, summary_scope=rig.scope)) == 1
    assert rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope) == []
    assert rig.index.summary_reference(rig.scope)[1] == ("阿青：居住地点是栖松巷。",)
    with sqlite3.connect(rig.path) as connection:
        rows = connection.execute("SELECT id, superseded_by FROM memory_summary_facts ORDER BY id")
        assert rows.fetchall() == [(old.identifier, old.identifier + 1), (old.identifier + 1, None)]


def test_fact_maintenance_rechecks_version_session_and_evidence(rig: Rig) -> None:
    excerpt = "我目前住在晴桂巷。"
    rig.add("m1", excerpt)
    rig.add("m2", "合成上下文")
    window = rig.window()
    delta = parse_delta(_answer("晴桂巷", excerpt), window)
    assert rig.index.commit_summary(window, delta, rig.source.check_window)
    [hit] = rig.index.search(["晴桂巷"], (), 5, summary_scope=rig.scope)
    assert hit.source is not None
    fact_source = hit.source
    rig.index.summary_scope_allowed = rig.source.allows_scope
    app = FastAPI()
    visible = True
    active = True

    async def ready() -> str:
        return "fixture-session"

    def can_show(scope: SummaryScope) -> bool:
        return visible and scope == rig.scope

    register_resource_routes(app, ready, None, rig.index, can_show_summary=can_show)
    register_memory_lifecycle_routes(
        app, ready, rig.index, lambda _: True, lambda _: active, can_show
    )
    endpoint = "/api/resources/memory/maintenance"
    catalog = "/api/resources/memory"
    with TestClient(app) as client:

        def preview(action: str, text: str | None = None) -> str:
            response = client.post(
                endpoint + "/preview",
                json={
                    "source": fact_source.encode(),
                    "action": action,
                    "text": text,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["plan_id"]

        expired = preview("correct", "阿青：居住地点是栖松巷。")
        with sqlite3.connect(rig.path) as database:
            assert database.execute("SELECT count(*) FROM memory_edits").fetchone() == (0,)
        rig.clock.advance(timedelta(minutes=15))
        assert client.post(f"{endpoint}/{expired}/apply").status_code == 409
        corrected = preview("correct", "阿青：居住地点是栖松巷。")
        race = preview("correct", "阿青：居住地点是竞争值。")
        visible = False
        assert client.post(f"{endpoint}/{corrected}/apply").status_code == 404
        assert client.get(catalog, params={"category": "fact_claims"}).json()["counts"] == {}
        visible = True
        active = False
        assert client.post(f"{endpoint}/{corrected}/apply").status_code == 404
        active = True
        detail_path = catalog + "/facts/detail"
        location = {"source": hit.source.encode()}
        visible = False
        for path in (detail_path, endpoint + "/history"):
            denied = client.get(path, params=location)
            assert denied.status_code == 404 and "total" not in denied.json()
        assert (
            client.post(
                endpoint + "/preview",
                json=location
                | {
                    "action": "correct",
                    "text": "未获准的修订",
                },
            ).status_code
            == 404
        )
        visible = True
        assert client.post(f"{endpoint}/{corrected}/apply").status_code == 200
        assert client.post(f"{endpoint}/{corrected}/apply").status_code == 409
        assert client.post(f"{endpoint}/{race}/apply").status_code == 409
        assert not rig.index.search(["晴桂巷"], (), 5, summary_scope=rig.scope)
        assert "栖松巷" in rig.index.search(["栖松巷"], (), 5, summary_scope=rig.scope)[0].text
        assert rig.index.summary_reference(rig.scope)[1:] == ((), ())
        assert client.get(catalog, params={"q": "晴桂巷"}).json()["items"] == []
        assert client.post(f"{endpoint}/{preview('disable')}/apply").status_code == 200
        removed = preview("remove")
        assert client.post(f"{endpoint}/{removed}/apply").status_code == 200
        assert client.get(catalog, params={"removed": True}).json()["items"][0]["is_removed"]
        assert not rig.index.search(["栖松巷"], (), 5, summary_scope=rig.scope)
        restored = preview("restore")
        assert client.post(f"{endpoint}/{restored}/apply").status_code == 200
        assert not rig.index.search(["栖松巷"], (), 5, summary_scope=rig.scope)
        assert client.post(f"{endpoint}/{removed}/undo").status_code == 409
        assert client.post(f"{endpoint}/{corrected}/undo").status_code == 409
        history = endpoint + "/history"
        params = {"source": hit.source.encode(), "limit": 2}
        page = client.get(history, params=params).json()
        assert page["total"] == 4
        assert [item["plan_id"] for item in page["items"]] == [restored, removed]
        assert [item["can_undo"] for item in page["items"]] == [True, False]
        assert client.post(f"{endpoint}/{restored}/undo").status_code == 200
        assert client.get(history, params=params).json()["items"][0]["undone_at"] is not None
        assert client.get(catalog, params={"removed": True}).json()["total"] == 1
        visible = False
        denied = client.get(history, params=params)
        assert denied.status_code == 404 and "total" not in denied.json()


@pytest.mark.parametrize("state", ("missing", "superseded"))
def test_unavailable_summary_fact_maintenance_is_not_found(rig: Rig, state: str) -> None:
    excerpt = "我目前住在晴桂巷。"
    rig.add("m1", excerpt)
    rig.add("m2", "合成上下文")
    window = rig.window()
    assert rig.index.commit_summary(
        window, parse_delta(_answer("晴桂巷", excerpt), window), rig.source.check_window
    )
    [hit] = rig.index.search(["晴桂巷"], (), 5, summary_scope=rig.scope)
    assert hit.source is not None
    rig.index.summary_scope_allowed = rig.source.allows_scope
    source = hit.source

    def can_show(scope: SummaryScope) -> bool:
        return scope == rig.scope

    applied = rig.index.preview_lifecycle(source, "disable", None, can_show, lambda: True)
    pending = rig.index.preview_lifecycle(source, "remove", None, can_show, lambda: True)
    assert applied is not None and pending is not None
    assert rig.index.apply_lifecycle(str(applied["plan_id"]), can_show, lambda: True, undo=False)
    with sqlite3.connect(rig.path) as database:
        if state == "missing":
            database.execute("DELETE FROM inbox_messages WHERE message_id = 'm1'")
        else:
            database.execute("UPDATE memory_summary_facts SET superseded_by = id + 1")

    app = FastAPI()

    async def ready() -> str:
        return "fixture-session"

    register_memory_lifecycle_routes(
        app, ready, rig.index, lambda _: True, lambda _: True, can_show
    )
    endpoint = "/api/resources/memory/maintenance"
    location = {"source": source.encode()}
    with TestClient(app) as client:
        denied = client.get(endpoint + "/history", params=location)
        assert denied.status_code == 404 and "total" not in denied.json()
        preview = client.post(endpoint + "/preview", json=location | {"action": "enable"})
        assert preview.status_code == 404
        assert client.post(f"{endpoint}/{pending['plan_id']}/apply").status_code == 404
        assert client.post(f"{endpoint}/{applied['plan_id']}/undo").status_code == 404


def test_lifecycle_history_follows_application_order_not_preview_order(rig: Rig) -> None:
    source = MemorySource("fixture", "formal", "fact_claims", (26,))
    assert rig.index.publish([HistoricalMemory(source, "原值")]) == 1
    first = rig.index.preview_lifecycle(source, "disable", None, None, lambda: True)
    second = rig.index.preview_lifecycle(source, "correct", "后预览先应用", None, lambda: True)
    assert first is not None and second is not None
    earlier, later = str(first["plan_id"]), str(second["plan_id"])
    assert rig.index.apply_lifecycle(later, None, lambda: True, undo=False)
    rig.clock.advance(timedelta(seconds=1))
    current = rig.index.preview_lifecycle(source, "disable", None, None, lambda: True)
    assert current is not None
    latest = str(current["plan_id"])
    rig.clock.advance(timedelta(seconds=1))
    assert rig.index.apply_lifecycle(latest, None, lambda: True, undo=False)
    with pytest.raises(LifecycleConflictError):
        rig.index.apply_lifecycle(earlier, None, lambda: True, undo=False)
    page = rig.index.lifecycle_history(source, None, lambda: True, 0, 10)
    assert page is not None
    items, total = page
    assert total == 2 and [item.plan_id for item in items] == [latest, later]
    assert [item.can_undo for item in items] == [True, False]
    with sqlite3.connect(rig.path) as database:
        database.execute(
            "UPDATE memory_lifecycle_plans SET applied_at=? WHERE id=?",
            ((rig.clock.now() - timedelta(minutes=1)).isoformat(), latest),
        )
    items, _total = rig.index.lifecycle_history(source, None, lambda: True, 0, 10) or ([], 0)
    assert [item.plan_id for item in items] == [later, latest]


def test_relation_maintenance_cannot_be_bypassed_by_old_endpoints(rig: Rig) -> None:
    first = MemorySource("first", "formal", "relations", (7,))
    second = MemorySource("second", "formal", "relations", (7,))
    assert (
        rig.index.publish(
            [
                HistoricalMemory(
                    first, "阿明却是河岸向导", actor_label="阿明", person_key="first:7"
                ),
                HistoricalMemory(
                    second, "阿明却是远山向导", actor_label="阿明", person_key="second:7"
                ),
            ]
        )
        == 2
    )
    items, total, _counts = rig.index.browse_text_resources("", "relations", "", "", 0, 10)
    assert total == 2
    ids = {item["source"]["instance"]: item["entry_id"] for item in items}
    assert not rig.index.update_resource("history", ids["first"], "绕过预览", None)
    assert not rig.index.set_resources_removed([("history", ids["first"])], True)

    for action in ("freeze", "protect"):
        plan = rig.index.preview_lifecycle(first, action, None, None, lambda: True)
        assert plan is not None
        relation_plan = str(plan["plan_id"])
        assert rig.index.apply_lifecycle(relation_plan, None, lambda: True, undo=False)
        assert not rig.index.update_resource("history", ids["first"], "晚到覆盖", None)
        assert not rig.index.set_resources_removed([("history", ids["first"])], True)
        independent = rig.index.preview_lifecycle(second, "disable", None, None, lambda: True)
        assert independent is not None
        identifier = str(independent["plan_id"])
        assert rig.index.apply_lifecycle(identifier, None, lambda: True, undo=False)
        assert rig.index.apply_lifecycle(identifier, None, lambda: True, undo=True)
        assert rig.index.apply_lifecycle(relation_plan, None, lambda: True, undo=True)
    restored = rig.index.get_history(first)
    assert restored is not None and restored.text == "阿明却是河岸向导"


@pytest.mark.parametrize("table", ("fact_claims", "episodes"))
def test_corrected_history_does_not_match_original_substring(rig: Rig, table: str) -> None:
    source = MemorySource("fixture", "formal", table, (25,))
    assert rig.index.publish([HistoricalMemory(source, "旧值由鸢尾石制成")]) == 1
    plan = rig.index.preview_lifecycle(source, "correct", "修订值由蓝铜片制成", None, lambda: True)
    assert plan is not None
    assert rig.index.apply_lifecycle(str(plan["plan_id"]), None, lambda: True, undo=False)
    assert rig.index.browse_text_resources("鸢尾石", table, "", "", 0, 10) == ([], 0, {})
    items, total, counts = rig.index.browse_text_resources("蓝铜片", table, "", "", 0, 10)
    assert total == 1 and counts == {table: 1}
    assert items[0]["text"] == "修订值由蓝铜片制成"


def test_history_preview_and_undo_reject_a_value_changed_back_by_legacy_edits(rig: Rig) -> None:
    source = MemorySource("fixture", "formal", "fact_claims", (25,))
    assert rig.index.publish([HistoricalMemory(source, "最初的值")]) == 1
    [item] = rig.index.browse_history("", "", 0, 10)
    assert item.entry_id is not None
    preview = rig.index.preview_lifecycle(source, "correct", "修订值", None, lambda: True)
    assert preview is not None
    assert rig.index.update_resource("history", item.entry_id, None, False)
    assert rig.index.update_resource("history", item.entry_id, None, True)
    with pytest.raises(LifecycleConflictError):
        rig.index.apply_lifecycle(str(preview["plan_id"]), None, lambda: True, undo=False)
    current = rig.index.preview_lifecycle(source, "correct", "修订值", None, lambda: True)
    assert current is not None
    current_id = str(current["plan_id"])
    assert rig.index.apply_lifecycle(current_id, None, lambda: True, undo=False)
    assert rig.index.update_resource("history", item.entry_id, "更新的值", None)
    assert rig.index.update_resource("history", item.entry_id, "修订值", None)
    with pytest.raises(LifecycleConflictError):
        rig.index.apply_lifecycle(current_id, None, lambda: True, undo=True)


def test_older_plan_can_be_undone_after_newer_plan_is_undone(rig: Rig) -> None:
    source = MemorySource("fixture", "formal", "fact_claims", (27,))
    assert rig.index.publish([HistoricalMemory(source, "原值")]) == 1

    def preview(action: LifecycleAction, text: str | None = None) -> str:
        plan = rig.index.preview_lifecycle(source, action, text, None, lambda: True)
        assert plan is not None
        return str(plan["plan_id"])

    def run(plan: str, *, undo: bool = False) -> None:
        assert rig.index.apply_lifecycle(plan, None, lambda: True, undo=undo)
        rig.clock.advance(timedelta(seconds=1))

    older = preview("disable")
    run(older)
    newer = preview("correct", "较新的修订")
    run(newer)
    run(newer, undo=True)
    run(older, undo=True)
    [restored] = rig.index.browse_history("", "", 0, 10)
    assert restored.text == "原值" and restored.is_enabled

    late = preview("correct", "迟到的修订")
    interim = preview("disable")
    run(interim)
    run(interim, undo=True)
    run(late)
    [changed] = rig.index.browse_history("", "", 0, 10)
    assert changed.text == "迟到的修订" and changed.is_enabled


@pytest.mark.asyncio
async def test_failed_cursor_write_rolls_back_accepted_facts(rig: Rig) -> None:
    excerpt = "我目前住在晴桂巷。"
    rig.add("m1", excerpt)
    rig.add("m2", "配套聊天上下文")

    async def answer(request: OrganRequest) -> OrganResponse:
        assert excerpt in request.prompt
        return OrganResponse(request.organ_id, _answer("晴桂巷", excerpt))

    with sqlite3.connect(rig.path) as connection:
        connection.execute("""CREATE TRIGGER synthetic_write_error
            BEFORE INSERT ON memory_summary_cursors BEGIN
                SELECT RAISE(ABORT, 'synthetic rollback');
            END""")
    assert not (await rig.runner.run(answer, _PREFERENCE)).completed
    assert rig.index.summary_reference(rig.scope) == (0, (), ())
    with sqlite3.connect(rig.path) as connection:
        assert connection.execute("SELECT count(*) FROM memory_summary_facts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM memory_summary_deltas").fetchone() == (0,)
        connection.execute("DROP TRIGGER synthetic_write_error")
    assert (await rig.runner.run(answer, _PREFERENCE)).completed
    assert rig.index.summary_reference(rig.scope)[0] == 2
