"""合成资料验证资源目录的业务分类、稳定定位与只读边界。"""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.common.sqlite import open_connection
from aureus.memory import MemoryEntry, MemoryIndex
from aureus.memory.history import HistoricalMemory, MemoryEvidence, MemorySource
from aureus.people import HistoricalKind, HistoricalRecord, PeopleDirectory
from aureus.timeline import ActorType, TimelineDatabase, TimelineRecord
from aureus.web.resource_api import register_resource_routes


async def _require_session(request: Request) -> str:
    if request.cookies.get("session") != "test":
        raise HTTPException(401)
    return "test"


def test_message_catalog_uses_same_category_without_crossing_edit_ids(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    memory = MemoryIndex(path, timeline, clock)
    memory.open()
    stamp = clock.now()
    source = MemorySource("demo", "main", "mai_messages", (71,))
    evidence = (MemoryEvidence("message", "sample:71"),)
    live = timeline.append("message", "同期现场消息", ActorType.USER)
    timeline.append("diary_written", "同期日记条目", ActorType.ME)
    unindexed = timeline.append("message", "同期未入索引消息", ActorType.USER)

    def describe(record: TimelineRecord) -> MemoryEntry | None:
        return None if record.id == unindexed.id else MemoryEntry("合成人物", record.content)

    try:
        memory.index_more(10, describe)
        memory.publish(
            [
                HistoricalMemory(source, "同期迁入消息", recorded_at=stamp, evidence=evidence),
                HistoricalMemory(
                    MemorySource("demo", "memory", "fact_claims", (1,)),
                    "过期事实原件",
                    valid_to=stamp - timedelta(days=1),
                ),
                HistoricalMemory(
                    MemorySource("demo", "memory", "image_assets", (1,)),
                    "同期图片原件",
                    recorded_at=stamp,
                ),
            ]
        )
        app = FastAPI()
        register_resource_routes(app, _require_session, None, memory)
        with TestClient(app) as client:
            endpoint = "/api/resources/memory"
            params = {"q": "同期", "category": "messages", "limit": 1}
            assert client.get(endpoint, params=params).status_code == 401
            client.cookies.set("session", "test")
            first = client.get(endpoint, params=params).json()
            assert first == client.get(endpoint, params={**params, "kind": "all"}).json()
            [history] = first["items"]
            assert first["total"] == 2 and first["counts"] == {"messages": 2}
            assert first["next_offset"] == 1 and history["labels"] == ["messages"]
            assert (
                history["kind"] == "history" and history["evidence"][0]["reference"] == "sample:71"
            )

            second = client.get(endpoint, params={**params, "offset": 1}).json()
            assert (
                second == client.get(endpoint, params={**params, "kind": "all", "offset": 1}).json()
            )
            [local] = second["items"]
            assert second["total"] == 2 and second["next_offset"] is None
            assert local["kind"] == "live" and local["labels"] == ["messages"]
            assert local["entry_id"] == history["entry_id"] == live.id
            legacy_history = client.get(
                endpoint, params={"kind": "history", "source_table": "mai_messages", "q": "同期"}
            ).json()
            assert legacy_history["total"] == 1 and not legacy_history["next_offset"]
            assert [item["text"] for item in legacy_history["items"]] == ["同期迁入消息"]
            legacy_live = client.get(endpoint, params={"kind": "live", "q": "现场"}).json()
            assert legacy_live["total"] == 1 and not legacy_live["next_offset"]
            assert [item["text"] for item in legacy_live["items"]] == ["同期现场消息"]
            for excluded in ("同期未入索引消息", "同期图片原件"):
                assert client.get(endpoint, params={"q": excluded}).json()["total"] == 0
            assert client.get(endpoint, params={"kind": "all"}).json()["counts"] == {
                "messages": 2,
                "experiences": 1,
                "fact_claims": 1,
            }

            target = {"kind": local["kind"], "entry_id": local["entry_id"]}
            assert client.patch(
                endpoint, json={**target, "text": "正文已修订", "is_enabled": False}
            ).is_success
            active = client.get(endpoint, params={**params, "recall": "enabled"}).json()
            assert active["total"] == 1 and active["items"][0]["text"] == "同期迁入消息"
            disabled = client.get(
                endpoint, params={**params, "q": "正文已修订", "recall": "disabled"}
            ).json()
            [item] = disabled["items"]
            assert item["text"] == "正文已修订" and item["original_text"] == "同期现场消息"
            assert not memory.search(["正文已修订"], set(), 5)
            assert client.patch(endpoint, json={**target, "is_enabled": True}).is_success

            memory.close()
            with closing(open_connection(path)) as conn, conn:
                conn.execute("DROP TABLE memory_index")
                conn.execute("DROP TABLE memory_index_state")
            memory.open()
            memory.index_more(10, describe)
            assert [hit.text for hit in memory.search(["正文已修订"], set(), 5)] == ["正文已修订"]
            assert not memory.search(["同期现场消息"], set(), 5)
            original = memory.get_history(source)
            assert original is not None and original.text == "同期迁入消息"
            assert original.evidence == evidence
            facts = client.get(endpoint, params={"kind": "all", "category": "fact_claims"}).json()
            assert facts["items"][0]["is_recallable"] is False
    finally:
        memory.close()
        timeline.close()


def test_people_catalog_preserves_exact_accounts_and_pending_profiles(tmp_path: Path) -> None:
    path = tmp_path / "people.db"
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    people = PeopleDirectory(path, clock)
    people.open()
    try:
        people.publish_history(
            [
                HistoricalRecord(
                    instance="demo",
                    kind=HistoricalKind.PROFILE,
                    source_key="[1]",
                    subject_key="[1]",
                    account=("qq", "synthetic-a"),
                    nickname="同名",
                ),
                HistoricalRecord(
                    instance="demo",
                    kind=HistoricalKind.PROFILE,
                    source_key="[2]",
                    subject_key="[2]",
                    account=("qq", "synthetic-b"),
                    nickname="同名",
                ),
                HistoricalRecord(
                    instance="demo",
                    kind=HistoricalKind.PROFILE_SNAPSHOT,
                    source_key="[3]",
                    subject_key="[1]",
                    nickname="同名",
                    summary="第一份画像",
                    evidence=[71],
                ),
                HistoricalRecord(
                    instance="demo",
                    kind=HistoricalKind.PROFILE_OVERRIDE,
                    source_key="[4]",
                    subject_key="[missing]",
                    nickname="同名",
                    summary="待关联画像",
                ),
            ]
        )
        first_person = people.find("qq", "synthetic-a")
        second_person = people.find("qq", "synthetic-b")
        assert first_person is not None and second_person is not None
        assert first_person.id != second_person.id
        app = FastAPI()
        register_resource_routes(app, _require_session, None, None, people=people)
        with TestClient(app) as client:
            endpoint = "/api/resources/profiles"
            assert client.get(endpoint).status_code == 401
            client.cookies.set("session", "test")
            first = client.get(endpoint, params={"limit": 2}).json()
            assert first["total"] == 4 and first["next_offset"] == 2
            second = client.get(endpoint, params={"limit": 2, "offset": 2}).json()
            assert second["total"] == 4 and second["next_offset"] is None
            assert {item["person_id"] for item in first["items"]} == {
                first_person.id,
                second_person.id,
            }
            assert all(
                item["entry_type"] == "person" and "record" not in item for item in first["items"]
            )
            profiles = {item["record"]["source_key"]: item for item in second["items"]}
            assert profiles["[3]"]["person_id"] == first_person.id
            assert profiles["[3]"]["record"]["evidence"] == [71]
            assert profiles["[4]"]["person_id"] is None and profiles["[4]"]["pending_reason"]
            assert client.get(endpoint, params={"q": "第一份画像"}).json()["total"] == 1
    finally:
        people.close()
