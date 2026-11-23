"""验证文字记忆交换与按原时间浏览。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.memory import HistoricalMemory, MemoryEntry, MemoryEvidence, MemoryIndex, MemorySource
from aureus.people import HistoricalKind, HistoricalRecord, PeopleDirectory
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


def _client(memory: MemoryIndex, clock: ManualClock | None = None) -> TestClient:
    async def require_ready(request: Request) -> str:
        if request.cookies.get("session") != "test":
            raise HTTPException(401)
        return "test"

    def same_origin(request: Request) -> bool:
        return request.headers.get("origin") == str(request.base_url).rstrip("/")

    app = FastAPI()
    register_memory_transfer_routes(app, require_ready, same_origin, memory, clock)
    return TestClient(app)


def test_memory_flow_uses_real_events_and_filters_before_paging(tmp_path: Path) -> None:
    now = datetime(2026, 11, 23, tzinfo=UTC)
    clock = ManualClock(now)
    path = tmp_path / "flow.db"
    timeline, memory = _open_memory(path, clock)
    people = PeopleDirectory(path, clock)
    people.open()
    try:
        timeline.append("message", "今天的经历", ActorType.USER)
        memory.index_more(10, lambda record: MemoryEntry("用户", record.content))
        old = MemorySource("test", "memory", "episodes", (1,))
        unknown = MemorySource("test", "memory", "episodes", (2,))
        media = MemorySource("test", "memory", "image_assets", (3,))
        snapshot = ("test", "same-fingerprint")
        memory.publish(
            [
                HistoricalMemory(old, "前天的记忆", recorded_at=now - timedelta(days=2)),
                HistoricalMemory(unknown, "时间未记录"),
                HistoricalMemory(media, "图片资产", recorded_at=now),
            ],
            snapshot=snapshot,
        )
        people.publish_history(
            [
                HistoricalRecord(
                    instance="test",
                    kind=HistoricalKind.PROFILE,
                    source_key='["person"]',
                    subject_key="person",
                    account=("qq", "200"),
                ),
                HistoricalRecord(
                    instance="test",
                    kind=HistoricalKind.PROFILE_SNAPSHOT,
                    source_key="[1]",
                    subject_key="person",
                    profile_version=1,
                    occurred_at=(now - timedelta(days=2)).timestamp(),
                ),
                HistoricalRecord(
                    instance="test",
                    kind=HistoricalKind.PROFILE_SNAPSHOT,
                    source_key="[2]",
                    subject_key="person",
                    profile_version=2,
                ),
            ]
        )
        user = people.ensure_user("维护者", [("web", "local")])
        contact = people.imported_contact("qq", "200")
        assert contact is not None
        people.bind_verified_account(user, "qq", "200", contact)
        with _client(memory, clock) as client:
            assert client.get(FLOW).status_code == 401
            client.cookies.set("session", "test")
            first = client.get(FLOW, params={"limit": 1}).json()
            second = client.get(FLOW, params={"limit": 1, "offset": first["next_offset"]}).json()
            third = client.get(FLOW, params={"limit": 1, "offset": second["next_offset"]}).json()
            assert client.get(FLOW, params={"event_type": "message"}).status_code == 422
            assert client.get(FLOW, params={"start": "2026-11-01T00:00"}).status_code == 422
            reversed_range = {"start": now.isoformat(), "end": "2026-11-01T00:00Z"}
            assert client.get(FLOW, params=reversed_range).status_code == 422
            recent = client.get(FLOW, params={"hours": 24}).json()
            assert [entry["event_type"] for entry in recent["items"]] == ["person_merged"]
            assert client.get(FLOW, params={"conversation": "another"}).json()["items"] == []
            latest = client.get(FLOW, params={"event_type": "profile_updated", "limit": 1}).json()
        assert first["items"][0]["event_type"] == "person_merged"
        assert second["items"][0]["source"]["key"] == [1]
        assert second["items"][0]["event_type"] == "profile_created"
        assert third["items"][0]["recorded_at"] is None
        assert third["next_offset"] is None
        assert latest["items"] == third["items"]
    finally:
        people.close()
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
