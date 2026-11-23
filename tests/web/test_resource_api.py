"""验证资源浏览、溯源与只读保护。"""

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.memory import MemoryIndex
from aureus.memory.history import HistoricalMemory, MemoryEvidence, MemorySource
from aureus.timeline import TimelineDatabase
from aureus.web.resource_api import register_resource_routes


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
