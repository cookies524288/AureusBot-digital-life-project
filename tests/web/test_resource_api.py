"""验证资源浏览、溯源与只读保护。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.perception import MediaReference, MessageRoute, Perception, TrustLevel
from aureus.memory import (
    EXPRESSION_TABLE,
    JARGON_TABLE,
    ImportedLanguageRow,
    LanguageKind,
    LanguageProposal,
    LanguageSample,
    LanguageStore,
    MemoryIndex,
)
from aureus.memory.emoji import EmojiStore
from aureus.memory.history import HistoricalMemory, MemoryEvidence, MemorySource
from aureus.memory.language import ExpressionReviewStatus, LanguageContent, LanguagePackageRow
from aureus.organs import OrganRequest, OrganResponse
from aureus.timeline import TimelineDatabase, VisibilityScope
from aureus.web.resource_api import register_resource_routes


@pytest.mark.asyncio
async def test_image_resource_returns_ranked_sources_only_to_signed_in_user(tmp_path: Path) -> None:
    path = tmp_path / "images.db"
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    inbox = InboxStore(path, clock)
    calls = []

    async def embed(request: OrganRequest) -> OrganResponse:
        calls.append(request)
        vector = (1.0, 0.1) if request.embedding_inputs == (b"two",) else (1.0, 0.0)
        space = "other" if request.embedding_inputs == (b"three",) else "same"
        return OrganResponse(
            request.organ_id, "", metadata={"embedding_space": space}, embeddings=(vector,)
        )

    images = InboxImageIndex(path, embed, lambda: True)
    for name in ("one", "two", "three"):
        attachment = MediaReference("image", name)
        perception = Perception(
            "qq",
            "test",
            "图片",
            VisibilityScope.USER_ONLY,
            TrustLevel.VERIFIED_USER,
            route=MessageRoute("account", "private", "test", name),
            attachments=(attachment,),
        )
        inbox.add(perception, conversation_name="合成私聊")
        await images.index(perception, attachment, name.encode())
        await images.index(perception, attachment, name.encode())
    assert len(calls) == 3

    async def require_ready(request: Request) -> str:
        if request.cookies.get("session") != "test":
            raise HTTPException(401)
        return "test"

    app = FastAPI()
    register_resource_routes(app, require_ready, None, None, images)
    with TestClient(app) as client:
        endpoint = "/api/resources/images"
        assert client.get(endpoint).status_code == 401
        client.cookies.set("session", "test")
        first = client.get(endpoint, params={"limit": 1}).json()
        assert first["items"][0]["attachment"] == "three"
        second = client.get(endpoint, params={"before": first["next_before"]}).json()
        assert first["total"] == second["total"] == 3
        assert second["next_before"] is None
        [match] = client.get(endpoint, params={"similar_to": second["items"][1]["id"]}).json()[
            "items"
        ]
        assert match["attachment"] == "two" and match["conversation"] == "合成私聊"
        assert "vector" not in match and match["score"] > 0.9


def test_learning_resources_filter_and_page_real_observations(tmp_path: Path) -> None:
    language = LanguageStore(tmp_path / "language.db")
    language.open()
    for number, term in enumerate(("月兔冲线", "月兔加油"), start=1):
        sample = LanguageSample(f"timeline:{number}", number, "person:1", f"跑步时说{term}")
        language.observe(
            [sample], [LanguageProposal(LanguageKind.EXPRESSION, term, "庆祝跑步", (number,))]
        )
    for number in range(3, 7):
        sample = LanguageSample(f"timeline:{number}", number, "person:1", "小队集合 GO")
        language.observe(
            [sample], [LanguageProposal(LanguageKind.JARGON, "GO", "集合完成", (number,))]
        )

    async def require_ready() -> str:
        return "test"

    app = FastAPI()
    register_resource_routes(app, require_ready, language, None)
    with TestClient(app) as client:
        endpoint = "/api/resources/learning"
        params = {"q": "庆祝跑步", "limit": 1}
        first = client.get(endpoint, params=params).json()
        second = client.get(endpoint, params={**params, "offset": first["next_offset"]}).json()
        beyond = client.get(endpoint, params={**params, "offset": 2}).json()
        assert first["total"] == second["total"] == beyond["total"] == 2
        assert beyond["items"] == [] and beyond["next_offset"] is None
        assert first["enabled"] is True and first["next_offset"] == 1
        assert second["next_offset"] is None
        entries = first["items"] + second["items"]
        assert {entry["text"] for entry in entries} == {"月兔冲线", "月兔加油"}
        assert all(entry["example"].startswith("跑步时说") for entry in entries)
        jargon = client.get(endpoint, params={"kind": "jargon", "q": "go"}).json()
        [entry] = jargon["items"]
        assert entry["detail"] == "集合完成"
        assert entry["evidence_ids"] == [3, 4, 5, 6]


@pytest.mark.parametrize("kind", [LanguageKind.JARGON, LanguageKind.EXPRESSION])
def test_learning_browse_combines_explicit_conversation_and_filters(
    tmp_path: Path, kind: LanguageKind
) -> None:
    database = tmp_path / "language.db"
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    inbox = InboxStore(database, clock)
    keys = []
    for chat_kind, recipient, name in (
        ("group", "10", "同名群"),
        ("group", "20", "同名群"),
        ("private", "10", "私聊"),
    ):
        perception = Perception(
            "qq",
            "fixture",
            "合成消息",
            VisibilityScope.USER_ONLY,
            TrustLevel.VERIFIED_USER,
            route=MessageRoute("account", chat_kind, recipient, "1"),
        )
        inbox.add(perception, conversation_name=name)
        keys.append(ConversationKey.from_perception(perception).language_scope_key())
    first_group, second_group, private = keys
    language = LanguageStore(database)
    language.open()
    language.add_entry(kind, LanguageContent("旧资料", "合成场景"))
    for text, detail, targets in (
        ("群内", "合成场景", [first_group]),
        ("共享", "合成场景", [first_group, private]),
        ("另一群", "合成场景", [second_group]),
        ("未映射", "合成场景", []),
        ("其他用途", "跑步", [first_group]),
        ("停用", "合成场景", [first_group]),
        ("待审", "合成场景", [first_group] if kind == LanguageKind.EXPRESSION else []),
    ):
        package = LanguagePackageRow(
            LanguageContent(text, detail), 2, "同名群", json.dumps({"text": text})
        )
        assert language.save_package(kind, [package]) == 1
        key = f"package:{package.digest}"
        if targets:
            language.package_scope(kind, key, targets, confirm=True)
        if text == "停用":
            assert language.update_entry(kind, key, None, is_enabled=False)
        if kind == LanguageKind.EXPRESSION and text not in {"待审", "未映射"}:
            assert (
                language.review_expressions(
                    [key], ExpressionReviewStatus.APPROVED, "合成审核", clock.now()
                )
                == 1
            )
    initial = language.entries(kind, include_disabled=True)

    def references() -> list[str]:
        return [
            language.jargon_reference(
                ["群内 共享 另一群 未映射 旧资料"], 2000, conversation_key=key
            )
            if kind == LanguageKind.JARGON
            else language.expression_reference(
                "合成场景", "合成场景", (), "fixture", 2000, conversation_key=key
            )
            for key in keys
        ]

    before = references()

    async def require_ready() -> str:
        return "test"

    app = FastAPI()
    register_resource_routes(app, require_ready, language, None, inbox=inbox)
    endpoint = "/api/resources/learning"
    with TestClient(app) as client:
        targets = {
            item["key"]: item["label"] for item in client.get(f"{endpoint}/scope").json()["items"]
        }
        assert set(targets) == set(keys)
        assert all(targets[key].startswith("同名群") for key in keys[:2])
        assert targets[first_group] != targets[second_group]
        params = {"kind": kind, "q": "合成场景", "recall": "enabled", "limit": 1}
        if kind == LanguageKind.EXPRESSION:
            params["review"] = "approved"
        first = client.get(endpoint, params={**params, "conversation_key": first_group}).json()
        assert first["total"] == 2 and first["next_offset"] == 1
        second = client.get(
            endpoint, params={**params, "conversation_key": first_group, "offset": 1}
        ).json()
        assert second["total"] == 2 and second["next_offset"] is None
        assert {item["text"] for item in first["items"] + second["items"]} == {"群内", "共享"}
        for key, text in ((second_group, "另一群"), (private, "共享")):
            result = client.get(endpoint, params={**params, "conversation_key": key}).json()
            assert result["total"] == 1 and result["next_offset"] is None
            assert [item["text"] for item in result["items"]] == [text]
        all_entries = client.get(endpoint, params={"kind": kind}).json()
        assert {item["text"] for item in all_entries["items"]} >= {"未映射", "旧资料"}
        empty = client.get(endpoint, params={**params, "conversation_key": "同名群"}).json()
        assert empty["total"] == 0 and empty["items"] == []
    assert language.entries(kind, include_disabled=True) == initial
    assert references() == before


@pytest.mark.parametrize("kind", [LanguageKind.JARGON, LanguageKind.EXPRESSION])
def test_learning_origin_and_authorization_are_independent(
    tmp_path: Path, kind: LanguageKind
) -> None:
    database = tmp_path / "sources.db"
    clock = ManualClock(datetime(2026, 11, 24, tzinfo=UTC))
    TimelineDatabase(database, clock).open()
    inbox = InboxStore(database, clock)
    keys = []
    for route in (
        MessageRoute("bot", "group", "10", "5"),
        MessageRoute("bot", "group", "20", "5"),
        MessageRoute("bot", "private", "5", "5", temporary_group_id="10"),
    ):
        message = Perception(
            "qq",
            "fixture",
            "测试",
            VisibilityScope.USER_ONLY,
            TrustLevel.VERIFIED_USER,
            route=route,
        )
        inbox.add(message, conversation_name="同名群")
        keys.append(ConversationKey.from_perception(message).language_scope_key())
    origin, authorized, temporary = keys
    language = LanguageStore(database)
    language.open()
    source_table = JARGON_TABLE if kind == LanguageKind.JARGON else EXPRESSION_TABLE
    source_ref = '["fixture","main","mai_messages",[7]]'
    language.publish(
        "fixture",
        "synthetic",
        {source_table: 1},
        [
            ImportedLanguageRow(
                source_table,
                "record-1",
                {"session_id": origin, "session_id_dict": {origin: 3}},
                "received",
                text="迁入词",
                detail="合成场景",
                evidence=(source_ref,),
            )
        ],
    )
    for index, (word, key, detail) in enumerate(
        (
            ("在线甲", origin, "合成场景"),
            ("在线乙", origin, "合成场景"),
            ("在线其他", origin, "其他场景"),
            ("临时私聊所得", temporary, "合成场景"),
        )
    ):
        samples = [
            LanguageSample(f"sample-{record_id}", record_id, "fixture", f"测试 {word}")
            for record_id in range(index * 4 + 1, index * 4 + 5)
        ]
        language.observe(samples, ())
        presented = {sample.record_id: sample.content for sample in samples}
        inferences = language.observe_extraction(
            kind, key, samples, presented, [LanguageProposal(kind, word, detail, tuple(presented))]
        )
        for attempt in inferences:
            language.record_jargon_inference(attempt, "accepted", detail)
    packages = [
        LanguagePackageRow(
            LanguageContent(word, "合成场景"), 1, "同名群", json.dumps({"text": word})
        )
        for word in ("包内有授权", "包内待授权")
    ]
    language.save_package(kind, packages)
    language.package_scope(kind, f"package:{packages[0].digest}", [authorized], confirm=True)
    initial = language.entries(kind, include_disabled=True)

    def references() -> list[str]:
        if kind == LanguageKind.JARGON:
            return [
                language.jargon_reference(
                    ["在线甲 迁入词 包内有授权 包内待授权"], 2000, conversation_key=key
                )
                for key in keys
            ]
        return [" ".join(item.text for item in language.expression_candidates(key)) for key in keys]

    before = references()
    assert all("在线甲" in reference and "迁入词" in reference for reference in before)
    assert "包内有授权" in before[1] and "包内有授权" not in before[0]
    assert all("包内待授权" not in reference for reference in before)

    async def require_ready() -> str:
        return "test"

    app = FastAPI()
    register_resource_routes(app, require_ready, language, None, inbox=inbox)
    endpoint = "/api/resources/learning"
    with TestClient(app) as client:

        def browse(**filters: str | int) -> dict[str, Any]:
            return client.get(endpoint, params={"kind": kind, "q": "合成场景", **filters}).json()

        first = browse(origin_conversation_key=origin, limit=1)
        second = browse(origin_conversation_key=origin, offset=1, limit=1)
        assert first["total"] == second["total"] == 2
        assert first["next_offset"] == 1 and second["next_offset"] is None
        assert {item["text"] for item in first["items"] + second["items"]} == {"在线甲", "在线乙"}
        assert browse(origin_conversation_key=authorized)["total"] == 0
        assert browse(origin_conversation_key="同名群")["total"] == 0
        assert [item["text"] for item in browse(origin_conversation_key=temporary)["items"]] == [
            "临时私聊所得"
        ]
        assert browse(authorized_conversation_key=origin)["total"] == 0
        assert browse(authorized_conversation_key=temporary)["total"] == 0
        combined = browse(origin_conversation_key=origin, authorized_conversation_key=authorized)
        assert combined["total"] == 0
        grants = browse(authorized_conversation_key=authorized)
        assert [item["text"] for item in grants["items"]] == ["包内有授权"]
        assert browse(conversation_key=authorized) == grants
        assert browse(conversation_key=origin)["total"] == 2
        details = {item["text"]: item for item in browse()["items"]}
        assert details["迁入词"]["source_refs"] == ["record-1", source_ref]
        assert details["迁入词"]["learning_source"] is None
        assert details["包内待授权"]["pending_scope"]
        assert details["包内有授权"]["package_source"] == "同名群"
        assert details["包内有授权"]["learning_source"] is None
        assert "包内待授权" not in {item["text"] for item in browse(recall="enabled")["items"]}
        origins = client.get(f"{endpoint}/origins").json()["items"]
        grants = client.get(f"{endpoint}/scope").json()["items"]
        assert {item["key"] for item in origins} == set(keys)
        assert {item["key"] for item in grants} == {origin, authorized}
    assert language.entries(kind, include_disabled=True) == initial
    assert references() == before


def test_resource_filters_pages_and_reads_evidence(tmp_path: Path) -> None:
    path = tmp_path / "resources.db"
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    memory = MemoryIndex(path, timeline, clock)
    memory.open()
    try:
        memory.publish(
            HistoricalMemory(
                MemorySource("test", "memory", "facts", (i,)),
                f"样本记忆 {i}",
                status="active" if i else "inactive",
                evidence=(MemoryEvidence("message", str(i)),),
            )
            for i in range(3)
        )

        async def require_ready(request: Request) -> str:
            if request.cookies.get("session") != "test":
                raise HTTPException(401)
            return "test"

        app = FastAPI()
        register_resource_routes(app, require_ready, None, memory)
        client = TestClient(app)
        endpoint = "/api/resources/memory"
        assert client.get(endpoint).status_code == 401
        client.cookies.set("session", "test")
        params = {"status": "active", "q": "样本", "limit": 1, "recall": "enabled"}
        first = client.get(endpoint, params=params).json()
        assert len(first["items"]) == 1
        assert first["items"][0]["evidence"][0]["kind"] == "message"
        assert first["next_offset"] == 1
        second = client.get(endpoint, params={**params, "offset": 1}).json()
        assert first["total"] == second["total"] == 2
        assert second["items"][0]["text"] != first["items"][0]["text"]
        assert second["next_offset"] is None
        empty = client.get(endpoint, params={"q": "不存在"}).json()
        assert empty["items"] == [] and empty["total"] == 0
        assert client.get("/api/resources/learning").json()["enabled"] is False
        assert timeline.count_records() == 0
    finally:
        memory.close()
        timeline.close()


def test_picture_catalog_filters_and_counts_the_unpaged_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "pictures.db"
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    memory = MemoryIndex(path, timeline, clock)
    memory.open()
    emojis = EmojiStore(path, tmp_path / "media", clock)
    archived = "a" * 64
    try:
        memory.publish(
            HistoricalMemory(
                MemorySource("fixture", "memory", "image_assets", (number,)),
                f"归档图片 {number}",
                evidence=(MemoryEvidence("attachment_archived", f"attachments/{archived}"),),
                recorded_at=clock.now(),
            )
            for number in range(2)
        )
        inbox = InboxStore(path, clock)

        async def embed(request: OrganRequest) -> OrganResponse:
            return OrganResponse(request.organ_id, "", embeddings=((1.0, 0.0),))

        images = InboxImageIndex(path, embed, lambda: True)
        for number in range(3):
            attachment = MediaReference("image", f"sample-{number}")
            perception = Perception(
                "qq",
                "fixture",
                "图片",
                VisibilityScope.USER_ONLY,
                TrustLevel.VERIFIED_USER,
                route=MessageRoute("account", "private", "fixture", str(number)),
                attachments=(attachment,),
            )
            inbox.add(perception, conversation_name=f"合成会话{number}")
            images._save(number + 1, f"sample-{number}", "fixture-space", (1.0, 0.0))

        async def require_ready() -> str:
            return "test"

        app = FastAPI()
        register_resource_routes(
            app, require_ready, None, memory, images, emojis=emojis, database=path
        )
        with TestClient(app) as client:
            endpoint = "/api/resources/pictures"
            first = client.get(endpoint, params={"availability": "reference", "limit": 1}).json()
            second = client.get(
                endpoint,
                params={"availability": "reference", "limit": 1, "offset": 1},
            ).json()
            archived_page = client.get(
                endpoint, params={"availability": "archived", "limit": 1}
            ).json()
            filtered = client.get(endpoint, params={"q": "sample-1"}).json()
            beyond = client.get(
                endpoint,
                params={"availability": "reference", "limit": 1, "offset": 3},
            ).json()
        assert first["total"] == second["total"] == 3
        assert first["next_offset"] == 1 and second["next_offset"] == 2
        assert archived_page["total"] == 2 and archived_page["items"][0]["digest"] == archived
        assert filtered["total"] == 1 and filtered["items"][0]["kind"] == "inbox"
        assert beyond["items"] == [] and beyond["total"] == 3 and beyond["next_offset"] is None
    finally:
        memory.close()
        timeline.close()
