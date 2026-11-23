"""用真实网页登录边界与合成时间线验证只读检修。"""

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.memory import HistoricalMemory, MemoryEntry, MemoryIndex, MemorySource
from aureus.timeline import ActorType, VisibilityScope
from aureus.web import create_app
from aureus.web.server import SESSION_COOKIE
from tests.web.test_server import FakeHeartbeat, Rig, set_password

INSPECTION = "/api/resources/memory/inspection"


def test_inspection_checks_only_authorized_originals_without_writing(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    database = tmp_path / "aureus.db"
    memory = MemoryIndex(database, rig.timeline, rig.clock)
    memory.open()
    try:

        def append(text: str, visibility: VisibilityScope) -> int:
            return rig.timeline.append("message", text, ActorType.USER, visibility=visibility).id

        enabled = append("合成用户正常原件", VisibilityScope.USER_ONLY)
        disabled = append("合成用户停用原件", VisibilityScope.USER_ONLY)
        mismatched = append("合成用户索引不匹配原件", VisibilityScope.USER_ONLY)
        group = append("合成群聊不可见原件", VisibilityScope.GROUP)
        private = append("合成内部不可见原件", VisibilityScope.PRIVATE)
        memory.index_more(10, lambda record: MemoryEntry("合成称呼", record.content))
        append("合成尚未入索引的原件", VisibilityScope.USER_ONLY)
        assert memory.update_resource("live", disabled, None, False)
        assert memory.update_resource("live", group, None, False)
        assert memory.set_resources_removed([("live", disabled), ("live", private)], True)
        memory.publish(
            [HistoricalMemory(MemorySource("合成源", "memory", "paragraphs", (1,)), "合成迁入")]
        )
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE memory_index SET visibility = 'group' WHERE rowid = ?", (mismatched,)
            )
            connection.execute(
                "UPDATE memory_index SET visibility = 'user_only' WHERE rowid = ?", (group,)
            )
            connection.executemany(
                "INSERT INTO memory_index(rowid, content, record_id, visibility) "
                "VALUES (?, ?, ?, 'user_only')",
                [(1000, "合成原件不存在", 1000), (1001, "合成错误引用", enabled)],
            )

        app = create_app(
            config=rig.config,
            auth=rig.auth,
            sessions=rig.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=rig.timeline,
            resource_memory=memory,
        )
        with TestClient(app) as client:
            assert client.get(INSPECTION).status_code == 401
            client.cookies.set(SESSION_COOKIE, rig.sessions.create())
            assert client.get(INSPECTION).status_code == 403
            set_password(rig)
            rig.clock.advance(timedelta(minutes=1))
            with closing(sqlite3.connect(database)) as connection:
                before = list(connection.iterdump())
                response = client.get(INSPECTION, params={"visibility": "group"})
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                report = response.json()
                assert set(report) == {"coverage", "checked", "checks"}
                assert "原件缺失" in report["coverage"]
                assert "零异常不代表全库完整" in report["coverage"]
                assert report["checked"] == 3
                assert {item["key"]: item["count"] for item in report["checks"]} == {
                    "disabled": 1,
                    "removed": 1,
                    "visibility_mismatch": 1,
                }
                assert all(item["label"] and item["detail"] for item in report["checks"])
                assert "合成" not in response.text
                assert client.post(INSPECTION).status_code == 405
                rig.clock.advance(timedelta(days=rig.config.session_days, minutes=-1))
                assert client.get(INSPECTION).status_code == 401
                assert list(connection.iterdump()) == before
    finally:
        memory.close()
        rig.close()


def test_inspection_distinguishes_no_coverage_from_read_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    database = tmp_path / "aureus.db"
    missing = tmp_path / "missing.db"
    memory = MemoryIndex(database, rig.timeline, rig.clock)
    memory.open()
    try:
        set_password(rig)
        token = rig.sessions.create()
        unavailable = MemoryIndex(missing, rig.timeline, rig.clock)
        for index, expected in [(memory, 200), (None, 409), (unavailable, 503)]:
            app = create_app(
                config=rig.config,
                auth=rig.auth,
                sessions=rig.sessions,
                channel=WebChannel(FakeHeartbeat()),
                timeline=rig.timeline,
                resource_memory=index,
            )
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE, token)
                response = client.get(INSPECTION)
                assert response.status_code == expected
                report = response.json()
                if expected == 200:
                    assert report["checked"] == 0
                    assert all(item["count"] == 0 for item in report["checks"])
                else:
                    assert set(report) == {"detail"}
        assert not missing.exists()
    finally:
        memory.close()
        rig.close()
