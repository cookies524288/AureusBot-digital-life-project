"""验证资源浏览、溯源与只读保护。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_store import InboxStore
from aureus.heartbeat.perception import MediaReference, MessageRoute, Perception, TrustLevel
from aureus.memory import LanguageKind, LanguageProposal, LanguageSample, LanguageStore, MemoryIndex
from aureus.memory.history import HistoricalMemory, MemoryEvidence, MemorySource
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
        assert first["enabled"] is True and first["next_offset"] == 1
        assert second["next_offset"] is None
        entries = first["items"] + second["items"]
        assert {entry["text"] for entry in entries} == {"月兔冲线", "月兔加油"}
        assert all(entry["example"].startswith("跑步时说") for entry in entries)
        jargon = client.get(endpoint, params={"kind": "jargon", "q": "go"}).json()
        [entry] = jargon["items"]
        assert entry["detail"] == "集合完成"
        assert entry["evidence_ids"] == [3, 4, 5, 6]


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
        params = {"status": "active", "q": "样本", "limit": 1}
        first = client.get(endpoint, params=params).json()
        assert len(first["items"]) == 1
        assert first["items"][0]["evidence"][0]["kind"] == "message"
        assert first["next_offset"] == 1
        second = client.get(endpoint, params={**params, "offset": 1}).json()
        assert second["items"][0]["text"] != first["items"][0]["text"]
        assert second["next_offset"] is None
        assert client.get(endpoint, params={"q": "不存在"}).json()["items"] == []
        assert client.get("/api/resources/learning").json()["enabled"] is False
        assert timeline.count_records() == 0
    finally:
        memory.close()
        timeline.close()
