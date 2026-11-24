"""对真实 SQLite 和网页登录边界验证批量维护历史。"""

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.common import MemoryConfig
from aureus.memory import HistoricalMemory, MemoryEntry, MemoryEvidence, MemoryIndex, MemorySource
from aureus.timeline import ActorType, VisibilityScope
from aureus.web import create_app
from aureus.web.server import SESSION_COOKIE
from tests.web.test_server import FakeHeartbeat, Rig, set_password

OPERATIONS = "/api/resources/memory/operations"
BATCH = "/api/resources/memory/batch"
ORIGIN = {"origin": "http://testserver"}


def _client(rig: Rig, memory: MemoryIndex) -> TestClient:
    return TestClient(
        create_app(
            config=rig.config,
            auth=rig.auth,
            sessions=rig.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=rig.timeline,
            resource_memory=memory,
        )
    )


def _append(rig: Rig, visibility: VisibilityScope) -> int:
    return rig.timeline.append(
        "diary_written", "合成不可泄露正文", ActorType.ME, visibility=visibility
    ).id


def _target(entry_id: int, kind: str = "live") -> dict[str, int | str]:
    return {"kind": kind, "entry_id": entry_id}


def test_memory_operations_show_only_visible_changes_before_paging(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    database = tmp_path / "aureus.db"
    memory = MemoryIndex(database, rig.timeline, rig.clock)
    memory.open()
    try:
        first = _append(rig, VisibilityScope.USER_ONLY)
        second = _append(rig, VisibilityScope.USER_ONLY)
        legacy = _append(rig, VisibilityScope.USER_ONLY)
        group = _append(rig, VisibilityScope.GROUP)
        private = _append(rig, VisibilityScope.PRIVATE)
        message = rig.timeline.append(
            "message", "合成平台原件", ActorType.USER, visibility=VisibilityScope.USER_ONLY
        )
        assert memory.index_more(10, lambda record: MemoryEntry("合成", record.content)) == 6
        memory.publish(
            [HistoricalMemory(MemorySource("合成", "memory", "paragraphs", (1,)), "迁入")]
        )
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE memory_index SET visibility = 'user_only' WHERE rowid = ?", (group,)
            )
            connection.execute(
                "UPDATE memory_index SET visibility = 'group' WHERE rowid = ?", (first,)
            )
            connection.execute(
                "INSERT INTO memory_edits(kind, entry_id, is_removed) VALUES ('live', ?, 1)",
                (legacy,),
            )
        with _client(rig, memory) as client:
            assert client.get(OPERATIONS).status_code == 401
            client.cookies.set(SESSION_COOKIE, rig.sessions.create())
            assert client.get(OPERATIONS).status_code == 403
            set_password(rig)
            assert client.get(OPERATIONS).json() == {
                "enabled": True,
                "items": [],
                "total": 0,
                "next_offset": None,
            }
            mixed = {
                "removed": True,
                "targets": [_target(first), _target(second), _target(group), _target(1, "history")],
            }
            with_message = {**mixed, "targets": [*mixed["targets"], _target(message.id)]}
            assert client.patch(BATCH, json=with_message, headers=ORIGIN).status_code == 409
            assert client.get(OPERATIONS).json()["total"] == 0
            assert rig.timeline.get(message.id) == message
            assert (
                client.patch(
                    BATCH, json=mixed, headers={"origin": "http://foreign.test"}
                ).status_code
                == 403
            )
            assert client.patch(BATCH, json=mixed, headers=ORIGIN).json() == {"changed": 4}
            assert client.patch(BATCH, json=mixed, headers=ORIGIN).status_code == 409
            assert (
                client.patch(
                    BATCH,
                    json={"removed": False, "targets": [_target(first), _target(999)]},
                    headers=ORIGIN,
                ).status_code
                == 409
            )
            assert client.patch(
                BATCH, json={"removed": True, "targets": [_target(private)]}, headers=ORIGIN
            ).json() == {"changed": 1}
            assert client.patch(
                BATCH, json={"removed": False, "targets": [_target(first)]}, headers=ORIGIN
            ).json() == {"changed": 1}
            stamp = rig.clock.now().isoformat()
            with closing(sqlite3.connect(database)) as connection:
                before = list(connection.iterdump())
                first_page = client.get(OPERATIONS, params={"limit": 1, "visibility": "group"})
                assert first_page.headers["cache-control"] == "no-store"
                assert first_page.json() == {
                    "enabled": True,
                    "items": [{"kind": "restore", "occurred_at": stamp, "changed": 1}],
                    "total": 2,
                    "next_offset": 1,
                }
                assert client.get(OPERATIONS, params={"offset": 1, "limit": 1}).json() == {
                    "enabled": True,
                    "items": [{"kind": "remove", "occurred_at": stamp, "changed": 2}],
                    "total": 2,
                    "next_offset": None,
                }
                assert client.get(OPERATIONS, params={"offset": -1}).status_code == 422
                assert client.get(OPERATIONS, params={"limit": 101}).status_code == 422
                rig.clock.advance(timedelta(days=rig.config.session_days))
                assert client.get(OPERATIONS).status_code == 401
                assert list(connection.iterdump()) == before
    finally:
        memory.close()
        rig.close()


def test_memory_operation_failure_rolls_back_and_switch_keeps_maintenance(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    database = tmp_path / "aureus.db"
    memory = MemoryIndex(database, rig.timeline, rig.clock)
    memory.open()
    try:
        entry_id = _append(rig, VisibilityScope.USER_ONLY)
        memory.index_more(10, lambda record: MemoryEntry("合成", record.content))
        assert memory.update_resource("live", entry_id, "合成修订", False)
        source = MemorySource("合成", "memory", "paragraphs", (1,))
        memory.publish(
            [HistoricalMemory(source, "合成原件", evidence=(MemoryEvidence("合成", "证据"),))]
        )
        remove = {"removed": True, "targets": [_target(entry_id), _target(1, "history")]}
        restore = remove | {"removed": False}
        set_password(rig)
        token = rig.sessions.create()
        with _client(rig, memory) as client, closing(sqlite3.connect(database)) as connection:
            client.cookies.set(SESSION_COOKIE, token)
            with connection:
                connection.execute("""CREATE TRIGGER reject_operation
                    BEFORE INSERT ON memory_removal_operations
                    BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
            before = list(connection.iterdump())
            assert client.patch(BATCH, json=remove, headers=ORIGIN).status_code == 503
            assert client.get(OPERATIONS).json()["total"] == 0
            assert list(connection.iterdump()) == before
            with connection:
                connection.execute("DROP TRIGGER reject_operation")
            originals = [
                line
                for line in connection.iterdump()
                if line.startswith(('INSERT INTO "timeline"', 'INSERT INTO "memory_history"'))
            ]
            assert client.patch(BATCH, json=remove, headers=ORIGIN).json() == {"changed": 2}
            disabled = MemoryIndex(
                database,
                rig.timeline,
                rig.clock,
                config=MemoryConfig(removal_operations_enabled=False),
            )
            with _client(rig, disabled) as disabled_client:
                disabled_client.cookies.set(SESSION_COOKIE, token)
                assert disabled_client.get(OPERATIONS).json() == {
                    "enabled": False,
                    "items": [],
                    "total": 0,
                    "next_offset": None,
                }
                assert disabled_client.patch(BATCH, json=restore, headers=ORIGIN).json() == {
                    "changed": 2
                }
            assert client.get(OPERATIONS).json()["total"] == 1
            assert connection.execute(
                "SELECT edited_content, is_enabled, is_removed "
                "FROM memory_edits WHERE kind = 'live'"
            ).fetchone() == ("合成修订", 0, 0)
            assert [
                line
                for line in connection.iterdump()
                if line.startswith(('INSERT INTO "timeline"', 'INSERT INTO "memory_history"'))
            ] == originals
        missing = tmp_path / "missing.db"
        with _client(rig, MemoryIndex(missing, rig.timeline, rig.clock)) as client:
            client.cookies.set(SESSION_COOKIE, token)
            assert client.get(OPERATIONS).status_code == 503
            assert not missing.exists()
    finally:
        memory.close()
        rig.close()
