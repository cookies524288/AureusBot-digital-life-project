"""验证文字记忆交换与按原时间浏览。"""

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.common import ManualClock
from aureus.memory import HistoricalMemory, MemoryEntry, MemoryEvidence, MemoryIndex, MemorySource
from aureus.people import HistoricalKind, HistoricalRecord, PeopleDirectory
from aureus.timeline import ActorType, TimelineDatabase
from aureus.web import create_app
from aureus.web.memory_transfer_api import register_memory_transfer_routes
from aureus.web.resource_api import register_resource_routes
from aureus.web.server import SESSION_COOKIE
from tests.web.test_server import FakeHeartbeat, Rig, set_password

FLOW = "/api/resources/memory/flow"
TRANSFER = "/api/resources/memory/transfer"
PREVIEW = TRANSFER + "/preview"
HEADERS = {"origin": "http://testserver", "content-type": "application/x-ndjson"}
EMPTY_COUNTS = {
    "messages": 0,
    "paragraphs": 0,
    "relations": 0,
    "fact_claims": 0,
    "episodes": 0,
    "other": 0,
}


def _open_memory(path: Path, clock: ManualClock) -> tuple[TimelineDatabase, MemoryIndex]:
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    memory = MemoryIndex(path, timeline, clock)
    memory.open()
    return timeline, memory


def _client(memory: MemoryIndex | None, clock: ManualClock | None = None) -> TestClient:
    async def require_ready(request: Request) -> str:
        if request.cookies.get("session") != "test":
            raise HTTPException(401)
        return "test"

    def same_origin(request: Request) -> bool:
        return request.headers.get("origin") == str(request.base_url).rstrip("/")

    app = FastAPI()
    register_memory_transfer_routes(
        app, require_ready, same_origin, memory, clock, require_preview_access=require_ready
    )
    register_resource_routes(app, require_ready, None, memory, same_origin=same_origin)
    return TestClient(app)


def test_memory_package_preview_counts_all_rows_before_confirming_real_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    timeline, memory = _open_memory(tmp_path / "source.db", clock)
    target_timeline, target = _open_memory(tmp_path / "target.db", clock)
    records = [
        HistoricalMemory(
            MemorySource("test", "memory", table, (number,)),
            "不应回显的合成原文",
            evidence=(MemoryEvidence("message", "不应回显的合成证据"),),
        )
        for number, table in enumerate(
            (
                "mai_messages",
                "messages",
                "paragraphs",
                "relations",
                "fact_claims",
                "episodes",
                "unknown",
            )
        )
    ]

    def reject_database(*args: object) -> None:
        pytest.fail("检视不应打开记忆库")

    try:
        memory.publish(records)
        package = BytesIO()
        memory.export_archive(package)
        data = package.getvalue()
        with _client(target) as client:
            client.cookies.set("session", "test")
            with monkeypatch.context() as patch:
                patch.setattr("aureus.memory.transfer.open_connection", reject_database)
                preview = client.post(PREVIEW, content=data, headers=HEADERS)
            assert preview.status_code == 200
            assert preview.headers["cache-control"] == "no-store"
            assert preview.json() == {
                "version": 1,
                "total": 7,
                "counts": {key: 2 if key == "messages" else 1 for key in EMPTY_COUNTS},
            }
            assert not target.browse_history("", "", 0, 20)
            assert client.post(TRANSFER, content=data, headers=HEADERS).json() == {
                "added": 7,
                "skipped": 0,
            }
            assert client.post(PREVIEW, content=data, headers=HEADERS).json() == preview.json()
            assert client.post(TRANSFER, content=data, headers=HEADERS).json() == {
                "added": 0,
                "skipped": 7,
            }
        assert target.get_history(records[0].source) == records[0]
    finally:
        target.close()
        target_timeline.close()
        memory.close()
        timeline.close()


def test_memory_package_preview_rejects_bad_tails_and_obeys_upload_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    timeline, memory = _open_memory(tmp_path / "memory.db", clock)
    try:
        empty = b'{"format":"aureus-memory","version":1}\n{"records":0}\n'
        package = BytesIO()
        memory.publish([HistoricalMemory(MemorySource("test", "memory", "episodes", (1,)), "合成")])
        memory.export_archive(package)
        header, row, trailer = package.getvalue().splitlines()
        with _client(memory) as client:
            assert client.post(PREVIEW, content=empty, headers=HEADERS).status_code == 401
            client.cookies.set("session", "test")
            assert client.post(PREVIEW, content=empty).status_code == 403
            assert (
                client.post(
                    PREVIEW, content=empty, headers={"origin": HEADERS["origin"]}
                ).status_code
                == 415
            )
            assert client.post(PREVIEW, content=empty, headers=HEADERS).json() == {
                "version": 1,
                "total": 0,
                "counts": EMPTY_COUNTS,
            }
            bad_tails = [b"\xff", b'{"records":2}', trailer + b"\n{}", b""]
            for tail in bad_tails:
                result = client.post(
                    PREVIEW, content=b"\n".join((header, row, tail)), headers=HEADERS
                )
                assert result.status_code == 422
                assert "合成" not in result.text
            assert (
                client.post(
                    PREVIEW, content=empty.replace(b'"version":1', b'"version":2'), headers=HEADERS
                ).status_code
                == 422
            )
            monkeypatch.setattr("aureus.web.memory_transfer_api._MAX_ARCHIVE_BYTES", len(empty) - 1)
            assert client.post(PREVIEW, content=empty, headers=HEADERS).status_code == 413
            assert client.post(TRANSFER, content=empty, headers=HEADERS).status_code == 413
        with _client(None) as client:
            client.cookies.set("session", "test")
            assert client.post(PREVIEW, content=empty, headers=HEADERS).status_code == 409
    finally:
        memory.close()
        timeline.close()


def test_memory_package_preview_requires_ready_session_without_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    memory = MemoryIndex(tmp_path / "aureus.db", rig.timeline, rig.clock)
    empty = b'{"format":"aureus-memory","version":1}\n{"records":0}\n'

    def reject_touch(token: str) -> bool:
        pytest.fail("检视不应更新登录状态")

    memory.open()
    try:
        app = create_app(
            config=rig.config,
            auth=rig.auth,
            sessions=rig.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=rig.timeline,
            resource_memory=memory,
        )
        with TestClient(app) as client:
            assert client.post(PREVIEW, content=empty, headers=HEADERS).status_code == 401
            client.cookies.set(SESSION_COOKIE, rig.sessions.create())
            assert client.post(PREVIEW, content=empty, headers=HEADERS).status_code == 403
            set_password(rig)
            monkeypatch.setattr(rig.sessions, "touch", reject_touch)
            assert client.post(PREVIEW, content=empty, headers=HEADERS).status_code == 200
    finally:
        memory.close()
        rig.close()


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
            assert recent["total"] == 1
            empty = client.get(FLOW, params={"conversation": "another"}).json()
            assert empty["items"] == [] and empty["total"] == 0
            latest = client.get(FLOW, params={"event_type": "profile_updated", "limit": 1}).json()
        assert first["items"][0]["event_type"] == "person_merged"
        assert second["items"][0]["source"]["key"] == [1]
        assert second["items"][0]["event_type"] == "profile_created"
        assert third["items"][0]["recorded_at"] is None
        assert first["total"] == second["total"] == third["total"] == 3
        assert third["next_offset"] is None
        assert latest["items"] == third["items"]
        assert latest["total"] == 1
    finally:
        people.close()
        memory.close()
        timeline.close()


def test_removed_text_is_omitted_from_the_ordinary_package(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    source_timeline, source = _open_memory(tmp_path / "source.db", clock)
    target_timeline, target = _open_memory(tmp_path / "target.db", clock)
    hidden = MemorySource("origin", "memory", "paragraphs", (1,))
    visible = MemorySource("origin", "memory", "paragraphs", (2,))
    hidden_record = HistoricalMemory(
        hidden, "隐藏望远镜", evidence=(MemoryEvidence("message", "7"),)
    )
    visible_record = HistoricalMemory(visible, "公开望远镜")
    try:
        source.publish([hidden_record, visible_record])
        with _client(source) as client:
            client.cookies.set("session", "test")
            endpoint = "/api/resources/memory"
            origin = {"origin": "http://testserver"}
            [item] = client.get(endpoint, params={"kind": "all", "q": "隐藏望远镜"}).json()["items"]
            body = {"targets": [{"kind": "history", "entry_id": item["entry_id"]}], "removed": True}
            assert client.patch(endpoint + "/batch", json=body, headers=origin).json() == {
                "changed": 1
            }
            package = client.get(TRANSFER).content
            header, row, trailer = map(json.loads, package.splitlines())
            assert header["version"] == 1 and trailer == {"records": 1}
            assert row["text"] == visible_record.text
            assert source.get_history(hidden) == hidden_record
            assert client.patch(
                endpoint + "/batch", json={**body, "removed": False}, headers=origin
            ).is_success
            assert json.loads(client.get(TRANSFER).content.splitlines()[-1]) == {"records": 2}
        with _client(target) as client:
            client.cookies.set("session", "test")
            assert client.post(TRANSFER, content=package, headers=HEADERS).json() == {
                "added": 1,
                "skipped": 0,
            }
            assert client.post(TRANSFER, content=package, headers=HEADERS).json() == {
                "added": 0,
                "skipped": 1,
            }
        assert target.get_history(hidden) is None
        assert target.get_history(visible) == visible_record
        assert [hit.source for hit in target.search(["望远镜"], set(), 10)] == [visible]
    finally:
        source.close()
        source_timeline.close()
        target.close()
        target_timeline.close()


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
            assert client.post(PREVIEW, content=conflict, headers=HEADERS).json()["total"] == 2
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
