"""3.3 正常命令端到端发布合成人际资料。"""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from aureus.cli import maibot_command
from aureus.cli.main import main
from aureus.common import ManualClock
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.core.runtime import open_core
from aureus.heartbeat import Perception, TrustLevel
from aureus.heartbeat.people_context import format_people_context
from aureus.heartbeat.recall import MAX_RECALL_CHARS
from aureus.organs import KeyResolver, OrganBus, OrganRequest
from aureus.people import HistoricalKind, PeopleDirectory, PeopleError
from aureus.timeline import VisibilityScope
from tests.cli.test_maibot_migration import SOURCE_ARGS, create_source, run_command
from tests.heartbeat.test_judging_flow import NOW
from tests.test_exploration_runtime import _paths
from tests.test_initiative_runtime import FixedOrgan


def _publish(staging: Path, home: Path, instance: str = SOURCE_ARGS[1]) -> int:
    return main(
        ["migrate-maibot", "publish-people", "--instance", instance, str(staging), str(home)]
    )


@pytest.mark.asyncio
async def test_publish_memory_profiles_and_recall_after_restart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    clock = ManualClock(NOW)
    timestamp = NOW.timestamp()
    expiry = timestamp + 60
    monkeypatch.setattr(maibot_command, "SystemClock", lambda: clock)
    with open_core(paths, clock=clock, fake_organs=True, to_console=False) as core:
        versions = core.identity_history.versions(limit=100)
        local_user = core.people.find("terminal", "local")
    original_identity = paths.identity_file.read_bytes()
    staging = tmp_path / "stage.sqlite"
    for role, schema in [
        (
            "main",
            """
            CREATE TABLE person_info (id INTEGER PRIMARY KEY, person_id, platform, user_id);
            INSERT INTO person_info VALUES
                (1,'a','qq','100'), (2,'b','qq','200'), (3,'c','qq','300');
            """,
        ),
        (
            "memory",
            f"""
            CREATE TABLE person_profile_snapshots (
                snapshot_id INTEGER PRIMARY KEY, person_id TEXT, profile_version INTEGER,
                profile_text TEXT, aliases_json TEXT, evidence_ids_json TEXT,
                updated_at REAL, expires_at REAL
            );
            INSERT INTO person_profile_snapshots VALUES
                (1,'a',3,'被覆盖的自动正文','["被清空的自动别名"]',NULL,{timestamp},{expiry}),
                (2,'a',2,'旧人工对象画像','["旧别名"]','["旧证据"]',{timestamp},{expiry}),
                (3,'b',9,'缺证据的最新版','["被覆盖的自动别名"]',NULL,{timestamp},{expiry}),
                (4,'b',8,'到期后也不能回落的旧版',NULL,NULL,{timestamp},{expiry + 600}),
                (5,'c',1,'无明确到期时刻的正文','["自动别名"]',NULL,{expiry - 21600},NULL),
                (6,'missing',1,'待关联画像',NULL,NULL,{timestamp},{expiry});
            CREATE TABLE person_profile_overrides (
                person_id TEXT PRIMARY KEY, override_text TEXT, updated_at REAL,
                updated_by TEXT, source TEXT
            );
            INSERT INTO person_profile_overrides VALUES ('a','人工正文',{timestamp},'测试','测试');
            CREATE TABLE person_profile_alias_overrides (
                person_id TEXT PRIMARY KEY, aliases_json TEXT, updated_at REAL,
                updated_by TEXT, source TEXT
            );
            INSERT INTO person_profile_alias_overrides VALUES
                ('a','[]',{timestamp},'测试','测试'),
                ('b','["人工别名"]',{timestamp},'测试','测试');
            """,
        ),
    ]:
        source, package = tmp_path / f"{role}.sqlite", tmp_path / f"{role}.zip"
        create_source(source, schema)
        assert run_command("export", role, source, package) == 0
        assert run_command("import", role, package, staging) == 0
    capsys.readouterr()
    original_staging = staging.read_bytes()
    for repeated in (False, True):
        assert _publish(staging, paths.home) == 0
        lines = capsys.readouterr()
        assert not lines.err and "人工正文" not in lines.out
        by_table = {row["table"]: row for row in json.loads(lines.out)["tables"]}
        for kind, count, pending in [
            (HistoricalKind.PROFILE_SNAPSHOT, 6, 1),
            (HistoricalKind.PROFILE_OVERRIDE, 1, 0),
            (HistoricalKind.ALIAS_OVERRIDE, 2, 0),
        ]:
            row = by_table[kind]
            assert (row["staged"], row["pending"]) == (count, pending)
            assert row["written"] == (0 if repeated else count - pending)
            assert row["already_exists"] == (count - pending if repeated else 0)
        assert by_table[HistoricalKind.PROFILE_SNAPSHOT]["reasons"] == {
            "unresolved_person_reference": 1
        }
    assert staging.read_bytes() == original_staging

    requests: list[OrganRequest] = []
    bus = OrganBus()
    for organ_id in ("planner", "replyer"):
        bus.register(FixedOrgan(organ_id, requests))

    def build_bus(
        _paths: object, _config: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    paths.runtime_config_file.write_text(
        "[body]\nsleep_enabled = false\n[heartbeat]\nplanning_enabled = true\n",
        encoding="utf-8",
    )
    with open_core(paths, clock=clock, to_console=False) as core:
        assert core.people.find("terminal", "local") == local_user
        assert core.identity_history.versions(limit=100) == versions
        alice = core.people.find("qq", "100")
        assert alice is not None
        old = core.people.historical_record(SOURCE_ARGS[1], HistoricalKind.PROFILE_SNAPSHOT, "[2]")
        empty = core.people.historical_record(
            SOURCE_ARGS[1], HistoricalKind.ALIAS_OVERRIDE, '["a"]'
        )
        assert old is not None and old.person_id == alice.id
        assert old.record.profile_version == 2
        assert old.record.original == {
            "snapshot_id": 2,
            "person_id": "a",
            "profile_version": 2,
            "profile_text": "旧人工对象画像",
            "aliases_json": '["旧别名"]',
            "evidence_ids_json": '["旧证据"]',
            "updated_at": timestamp,
            "expires_at": expiry,
        }
        assert empty is not None and empty.record.aliases == []
        assert len(core.people.history_for(alice.id)) == 5
        core.heartbeat.attach_channel("qq", lambda _: None)
        core.heartbeat.wake_up()
        for age, sender, expected, absent in [
            (1, "100", ("人工正文",), ("被覆盖的自动正文", "被清空的自动别名", "旧别名")),
            (2, "200", ("缺证据的最新版", "人工别名"), ("被覆盖的自动别名", "人工正文")),
            (3, "300", ("无明确到期时刻的正文", "自动别名"), ("人工正文",)),
            (60, "200", ("人工别名",), ("缺证据的最新版", "到期后也不能回落的旧版")),
            (61, "300", (), ("无明确到期时刻的正文", "自动别名")),
            (62, "100", ("人工正文",), ("被覆盖的自动正文", "被清空的自动别名")),
        ]:
            clock.advance(NOW + timedelta(seconds=age) - clock.now())
            requests.clear()
            assert core.heartbeat.submit(
                Perception(
                    "qq", sender, "今天聊聊吧", VisibilityScope.GROUP, TrustLevel.VERIFIED_CONTACT
                )
            )
            core.heartbeat.beat()
            await core.heartbeat.wait_for_thought()
            core.heartbeat.beat()
            reply = next(request for request in requests if request.context["task"] == "reply")
            assert all(item in reply.prompt for item in expected)
            assert all(item not in reply.prompt for item in absent)
        await core.heartbeat.shut_down()
        assert core.identity_history.versions(limit=100) == versions
    assert paths.identity_file.read_bytes() == original_identity


def test_publish_people_after_real_export_and_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    clock = ManualClock(NOW)
    monkeypatch.setattr(maibot_command, "SystemClock", lambda: clock)
    with open_core(paths, clock=clock, fake_organs=True, to_console=False) as core:
        versions = core.identity_history.versions(limit=100)
        local_user = core.people.find("terminal", "local")
        assert versions and local_user is not None and local_user.is_user
    original_identity = paths.identity_file.read_bytes()

    staging = tmp_path / "staging.sqlite"
    for role, schema in [
        (
            "main",
            """
            CREATE TABLE person_info (
                id PRIMARY KEY, person_id, platform, user_id, person_name, user_nickname,
                group_cardname, name_reason, memory_points, first_known_time, is_user
            );
            INSERT INTO person_info VALUES
                (1,'a','qq','123','同名','旧昵称','[{"group_id":"g","group_cardname":"旧群名"}]',
                 '来源称呼原因','["展览"]','2023-01-01 00:00:00',0),
                ('1','b','qq','456','同名','群名',NULL,NULL,NULL,NULL,1),
                (2,'c',NULL,NULL,'同名',NULL,NULL,NULL,NULL,NULL,0),
                (3,'d','terminal','local','旧用户称呼',NULL,NULL,NULL,NULL,NULL,0);
            """,
        ),
        (
            "people",
            """
            CREATE TABLE person_state (
                person_id TEXT PRIMARY KEY, display_name, affinity, mood, ignore, thread, updated_at
            );
            INSERT INTO person_state VALUES ('a','熟人称呼',0.31,'平静',0,'["兴趣话题"]',100.25);
            CREATE TABLE relation_edge (
                from_person, to_person, kind, weight, evidence, updated_at,
                PRIMARY KEY (from_person, to_person, kind)
            );
            INSERT INTO relation_edge VALUES
                ('a','b','合作',0.31,'一起准备展览',123.456),
                ('a','c','合作',-2.5,'保留未解析证据',NULL);
            CREATE TABLE events (id PRIMARY KEY, person_id, kind, ts, detail);
            INSERT INTO events VALUES
                (1,'a','对话','2026-11-20 10:30:00','{"text":"讨论展览行程"}');
            CREATE TABLE thread_item (
                id PRIMARY KEY, person_id, content, created_at, due_at, hardness
            );
            INSERT INTO thread_item VALUES (1,'b','补充展览细节',3.125,4.25,0.75);
            """,
        ),
    ]:
        source, package = tmp_path / f"{role}.sqlite", tmp_path / f"{role}.zip"
        create_source(source, schema)
        assert run_command("export", role, source, package) == 0
        assert run_command("import", role, package, staging) == 0
    capsys.readouterr()
    original_staging = staging.read_bytes()

    def fail_publication(*_args: object) -> None:
        raise PeopleError("private-instance")

    with monkeypatch.context() as patch:
        patch.setattr(PeopleDirectory, "publish_history", fail_publication)
        assert _publish(staging, paths.home) == 1
        assert json.loads(capsys.readouterr().err) == {"error": "people_source_conflict"}

    for repeated in (False, True):
        assert _publish(staging, paths.home) == 0
        captured = capsys.readouterr()
        assert not captured.err
        for private in ("private-instance", str(paths.home), "123", "展览", "同名"):
            assert private not in captured.out
        report = json.loads(captured.out)
        by_table = {row["table"]: row for row in report["tables"]}
        for table, count, pending in [
            ("person_info", 4, 1),
            ("person_state", 1, 0),
            ("relation_edge", 2, 1),
            ("events", 1, 0),
            ("thread_item", 1, 0),
        ]:
            row = by_table[table]
            assert (row["staged"], row["pending"]) == (count, pending)
            assert row["written"] == (0 if repeated else count - pending)
            assert row["already_exists"] == (count - pending if repeated else 0)
        assert by_table["person_info"]["reasons"] == {"missing_account": 1}
        assert by_table["relation_edge"]["reasons"] == {"unresolved_relation_endpoint": 1}
    assert staging.read_bytes() == original_staging
    assert paths.identity_file.read_bytes() == original_identity

    people = PeopleDirectory(paths.database_file, clock)
    people.open()
    try:
        alice, bob = people.find("qq", "123"), people.find("qq", "456")
        assert alice is not None and bob is not None and alice.id != bob.id
        assert not alice.is_user and not bob.is_user
        assert people.find("terminal", "local") == local_user
        a_history = {row.record.kind: row for row in people.history_for(alice.id)}
        b_history = {row.record.kind: row for row in people.history_for(bob.id)}
        profile = a_history[HistoricalKind.PROFILE].record
        assert profile.name == "同名" and profile.known_since == "2023-01-01 00:00:00"
        assert profile.nickname == "旧昵称"
        assert profile.group_cardname == '[{"group_id":"g","group_cardname":"旧群名"}]'
        assert profile.name_reason == "来源称呼原因"
        assert profile.memory_points == '["展览"]' and profile.source_key == "[1]"
        assert b_history[HistoricalKind.PROFILE].record.source_key == '["1"]'
        state = a_history[HistoricalKind.STATE].record
        assert state.name == "熟人称呼" and state.occurred_at == 100.25
        assert state.relationship_state == {
            "affinity": 0.31,
            "mood": "平静",
            "ignore": 0,
            "thread": '["兴趣话题"]',
        }
        edge = a_history[HistoricalKind.RELATION]
        assert b_history[HistoricalKind.RELATION] == edge
        assert edge.person_id == alice.id and edge.other_person_id == bob.id
        assert edge.record.relation_type == "合作"
        assert edge.record.weight == 0.31 and edge.record.evidence == "一起准备展览"
        assert edge.record.occurred_at == 123.456
        assert edge.record.original == {
            "from_person": "a",
            "to_person": "b",
            "kind": "合作",
            "weight": 0.31,
            "evidence": "一起准备展览",
            "updated_at": 123.456,
        }
        event = a_history[HistoricalKind.EVENT].record
        assert event.event_type == "对话" and event.occurred_at == "2026-11-20 10:30:00"
        assert event.summary == '{"text":"讨论展览行程"}'
        thread = b_history[HistoricalKind.THREAD_ITEM].record
        assert thread.summary == "补充展览细节" and thread.occurred_at == 3.125
        assert thread.due_at == 4.25 and thread.hardness == 0.75
        alice_context = format_people_context(list(a_history.values()), alice.id, MAX_RECALL_CHARS)
        bob_context = format_people_context(list(b_history.values()), bob.id, MAX_RECALL_CHARS)
        assert "关系方向：发出" in alice_context and "讨论展览行程" in alice_context
        assert "昵称：旧昵称" in alice_context and "事件：对话" in alice_context
        assert "关系方向：指向此人" in bob_context and "补充展览细节" in bob_context
        pending = people.historical_record(
            SOURCE_ARGS[1], HistoricalKind.RELATION, '["a","c","\\u5408\\u4f5c"]'
        )
        assert pending is not None and pending.record.evidence == "保留未解析证据"
        assert pending.record.occurred_at is None and pending.other_person_id is None
    finally:
        people.close()
    with open_core(paths, clock=clock, fake_organs=True, to_console=False) as core:
        assert core.identity_history.versions(limit=100) == versions


def test_publish_people_rejects_non_staging_and_missing_instance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _paths(tmp_path)
    source = tmp_path / "raw.sqlite"
    package = tmp_path / "raw.zip"
    staging = tmp_path / "stage.sqlite"
    create_source(source, "CREATE TABLE person_info (id PRIMARY KEY);")
    assert _publish(source, paths.home) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "not_staging_database"}
    assert run_command("export", "main", source, package) == 0
    assert run_command("import", "main", package, staging) == 0
    capsys.readouterr()
    assert _publish(staging, paths.home, "not-staged") == 1
    assert json.loads(capsys.readouterr().err) == {"error": "source_not_imported"}
    assert not paths.database_file.exists()
