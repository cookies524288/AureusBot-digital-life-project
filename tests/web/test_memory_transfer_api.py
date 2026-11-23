"""验证文字记忆交换与按原时间浏览。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.memory import HistoricalMemory, MemoryEntry, MemoryEvidence, MemoryIndex, MemorySource
from aureus.timeline import ActorType, TimelineDatabase
from aureus.web.memory_transfer_api import register_memory_transfer_routes

FLOW = "/api/resources/memory/flow"
TRANSFER = "/api/resources/memory/transfer"
HEADERS = {"origin": "http://testserver", "content-type": "application/x-ndjson"}


def _open_memory(path: Path, clock: ManualClock) -> tuple[TimelineDatabase, MemoryIndex]:
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    memory = MemoryIndex(path, timeline, clock)
    memory.open()
    return timeline, memory


def _client(memory: MemoryIndex) -> TestClient:
    async def require_ready(request: Request) -> str:
        if request.cookies.get("session") != "test":
            raise HTTPException(401)
        return "test"

    def same_origin(request: Request) -> bool:
        return request.headers.get("origin") == str(request.base_url).rstrip("/")

    app = FastAPI()
    register_memory_transfer_routes(app, require_ready, same_origin, memory)
    return TestClient(app)


def test_memory_flow_orders_and_pages_history_and_live(tmp_path: Path) -> None:
    now = datetime(2026, 11, 23, tzinfo=UTC)
    clock = ManualClock(now)
    timeline, memory = _open_memory(tmp_path / "flow.db", clock)
    try:
        local = timeline.append("message", "今天的经历", ActorType.USER)
        memory.index_more(10, lambda record: MemoryEntry("用户", record.content))
        old = MemorySource("test", "memory", "episodes", (1,))
        unknown = MemorySource("test", "memory", "episodes", (2,))
        media = MemorySource("test", "memory", "image_assets", (3,))
        memory.publish(
            [
                HistoricalMemory(old, "前天的记忆", recorded_at=now - timedelta(days=2)),
                HistoricalMemory(unknown, "时间未记录"),
                HistoricalMemory(media, "图片资产", recorded_at=now),
            ]
        )
        with _client(memory) as client:
            assert client.get(FLOW).status_code == 401
            client.cookies.set("session", "test")
            first = client.get(FLOW, params={"limit": 1}).json()
            second = client.get(FLOW, params={"limit": 1, "offset": first["next_offset"]}).json()
            third = client.get(FLOW, params={"limit": 1, "offset": second["next_offset"]}).json()
        assert first["items"][0]["record_id"] == local.id
        assert second["items"][0]["source"]["key"] == [1]
        assert third["items"][0]["recorded_at"] is None
        assert third["next_offset"] is None
    finally:
        memory.close()
        timeline.close()


def test_memory_file_roundtrip_and_conflicts_are_atomic(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    source_timeline, source = _open_memory(tmp_path / "source.db", clock)
    target_timeline, target = _open_memory(tmp_path / "target.db", clock)
    try:
        origin = MemorySource("origin", "memory", "paragraphs", ({"blob_base64": "AP8="}, 2))
        message = MemorySource("origin", "main", "mai_messages", (42,))
        original = HistoricalMemory(
            origin,
            "晚饭后跑步\n喝柠檬水",
            person_key="maibot:origin:test",
            recorded_at=clock.now(),
            evidence=(MemoryEvidence("message", "42", message),),
            received_at=clock.now(),
        )
        source.publish([original])
        [entry] = source.browse_history("", "", 0, 1)
        assert entry.entry_id is not None
        assert source.update_resource("history", entry.entry_id, "喝橙汁", False)
        with _client(source) as client:
            assert client.get(TRANSFER).status_code == 401
            client.cookies.set("session", "test")
            response = client.get(TRANSFER)
            assert response.status_code == 200
            assert response.headers["content-disposition"].endswith('"aureus-memory.jsonl"')
        with _client(target) as client:
            assert (
                client.post(TRANSFER, content=response.content, headers=HEADERS).status_code == 401
            )
            client.cookies.set("session", "test")
            assert client.post(TRANSFER, content=response.content).status_code == 403
            assert client.post(TRANSFER, content=response.content, headers=HEADERS).json() == {
                "added": 1,
                "skipped": 0,
            }
            assert client.post(TRANSFER, content=response.content, headers=HEADERS).json() == {
                "added": 0,
                "skipped": 1,
            }
            header, row, _ = response.content.splitlines()
            fresh = json.loads(row)
            fresh["source"]["key"] = [3]
            first = json.dumps(fresh).encode()
            collision = json.loads(row)
            collision["edited_text"] = "别的修订"
            second = json.dumps(collision).encode()
            conflict = b"\n".join([header, first, second, b'{"records":2}', b""])
            assert client.post(TRANSFER, content=conflict, headers=HEADERS).status_code == 409
            truncated = b"\n".join([header, first, b""])
            assert client.post(TRANSFER, content=truncated, headers=HEADERS).status_code == 422
            assert client.post(TRANSFER, content=b"../other.db", headers=HEADERS).status_code == 422
        assert target.get_history(origin) == original
        assert target.get_history(MemorySource("origin", "memory", "paragraphs", (3,))) is None
        [restored] = target.browse_history("", "", 0, 10)
        assert restored.text == "喝橙汁" and not restored.is_recallable
        assert restored.original_text == original.text
    finally:
        source.close()
        source_timeline.close()
        target.close()
        target_timeline.close()
