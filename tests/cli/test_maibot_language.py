"""由正式迁移 CLI 发布合成 MaiBot 语言，走正常规划和回复入口验证。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from aureus.cli.main import main
from aureus.common import ManualClock
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import Perception, TrustLevel
from aureus.memory import LanguageKind, LanguageSample, LanguageStore
from aureus.organs import KeyResolver, OrganBus, OrganRequest
from aureus.timeline import TimelineDatabase, VisibilityScope
from tests.cli.test_maibot_migration import create_source, run_command
from tests.heartbeat.test_judging_flow import NOW
from tests.prompt_assertions import dialogue_text
from tests.test_exploration_runtime import _paths
from tests.test_language_runtime import LanguageOrgan

_SCHEMA = """
CREATE TABLE mai_messages (id INTEGER PRIMARY KEY, platform TEXT, message_id TEXT,
    session_id TEXT, user_id TEXT, timestamp TEXT, processed_plain_text TEXT);
INSERT INTO mai_messages VALUES
    (1, 'mock', '01', 'group', 'runner', '2026-11-21 01:02:03', 'GG 抵达终点'),
    (2, 'mock', '02', NULL, 'runner', '2026-11-21 04:05:06', 'GG 等候开局');
CREATE TABLE expressions (id INTEGER PRIMARY KEY, situation TEXT, style TEXT,
    content_list TEXT, count INTEGER, checked INTEGER, modified_by TEXT, create_time TEXT,
    last_active_time TEXT, session_id TEXT, disabled INTEGER);
INSERT INTO expressions VALUES
    (1, '庆祝跑步成绩', '使用 白桦冲线', '["来庆祝跑步成绩"]', 12, 1, 'USER', 't0', 't1', NULL, 0),
    (2, '庆祝跑步成绩', '禁用表达', '["例句"]', 99, 1, NULL, NULL, NULL, 'group', 1),
    (3, '庆祝跑步成绩', '缺例表达', '[]', 1, 0, NULL, NULL, NULL, NULL, 0),
    (4, '庆祝跑步成绩', '使用 对对对', '["例句"]', 99, 1, NULL, NULL, NULL, NULL, 0),
    (5, '庆祝跑步成绩', '', '[]', 1, 0, NULL, NULL, NULL, NULL, 0);
CREATE TABLE jargons (id INTEGER PRIMARY KEY, content TEXT, meaning TEXT, is_jargon INTEGER,
    is_complete INTEGER, count INTEGER, last_inference_count INTEGER, evidence_messages TEXT);
INSERT INTO jargons VALUES
    (1, 'GG', '抵达终点', 1, 0, 4, 4, '[[{"platform":"mock","message_id":"01"}]]'),
    (2, 'gg', '等候开局', 1, 1, 5, 5, '[[{"platform":"mock","message_id":"02"}]]'),
    (3, 'GG', '证据未知', 1, 0, 4, 4, '[[{"platform":"mock","message_id":"03"}]]'),
    (4, 'GG', '已认定非黑话', 0, 1, 99, 99, '[]'),
    (5, 'GG', '', 0, 0, 2, 0, NULL),
    (6, 'GO', '普通提醒', 1, 1, 20, 20, '[[{"platform":"mock","message_id":"02"}]]'),
    (7, 'GG', '原库无引用', 1, 1, 3, 3, '[]');
CREATE TABLE high_frequency_terms (id INTEGER PRIMARY KEY, term TEXT, rank INTEGER,
    occurrence_count INTEGER, chat_id TEXT);
INSERT INTO high_frequency_terms VALUES (1, 'gG', 2, 7, 'group'), (2, 'GG', 1, 4, NULL),
    (3, '', 3, 10, 'group');
"""


def _publish(staging: Path, target: Path) -> int:
    return main(
        [
            "migrate-maibot",
            "publish-language",
            "--instance",
            "private-instance",
            str(staging),
            str(target),
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("old_target", [False, True], ids=["fresh", "old-target"])
async def test_cli_publishes_language_from_real_staging_then_restarts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    old_target: bool,
) -> None:
    paths = _paths(tmp_path)
    source, package, staging = (tmp_path / name for name in ("source.db", "pkg.zip", "staging.db"))
    create_source(source, _SCHEMA)
    assert run_command("export", "main", source, package) == 0
    assert run_command("import", "main", package, staging) == 0
    capsys.readouterr()

    timeline = TimelineDatabase(paths.database_file, ManualClock(NOW))
    timeline.open()
    timeline.close()
    staging_bytes = staging.read_bytes()
    identity_bytes = paths.identity_file.read_bytes()
    store = LanguageStore(paths.database_file)
    observed = LanguageSample("synthetic", 99, "person:1", "白桦冲线")
    if old_target:
        assert _publish(staging, paths.database_file) == 0
        capsys.readouterr()
        # 还原旧发布器实际保存的待处理行，并保留目标库的自定义及观察。
        create_source(
            paths.database_file,
            """
            UPDATE language_import_rows
            SET status = 'pending', reason = 'missing_example', text = '', detail = '', count = 0
            WHERE table_name = 'expressions' AND json_extract(raw_values, '$.id') = 3;
            UPDATE language_import_rows
            SET status = 'pending', reason = 'evidence_unresolved', count = 0
            WHERE table_name = 'jargons' AND json_extract(raw_values, '$.id') = 3;
            UPDATE language_import_rows
            SET status = 'pending', reason = 'missing_evidence'
            WHERE table_name = 'jargons' AND json_extract(raw_values, '$.id') = 7;
            UPDATE language_import_rows SET example = '本地例句'
            WHERE table_name = 'expressions' AND json_extract(raw_values, '$.id') = 1;
            """,
        )
        store.observe([observed], [])

    initial_counts = [(1, 1), (2, 3), (0, 2)] if old_target else [(2, 0), (5, 0), (2, 0)]
    for expected_counts in (initial_counts, [(0, 2), (0, 5), (0, 2)]):
        assert _publish(staging, paths.database_file) == 0
        stdout = capsys.readouterr().out
        assert all(text not in stdout for text in ("白桦冲线", "private-instance", "runner"))
        tables = json.loads(stdout)["tables"]
        for (table, (total, pending, skipped)), (received, exists) in zip(
            {
                "expressions": (5, 1, 2),
                "jargons": (7, 1, 1),
                "high_frequency_terms": (3, 1, 0),
            }.items(),
            expected_counts,
            strict=True,
        ):
            assert tuple(
                tables[table][field]
                for field in ("source", "received", "already_exists", "pending", "skipped")
            ) == (total, received, exists, pending, skipped)
        assert tables["jargons"]["unresolved_evidence"] == 1
        assert tables["jargons"]["reasons"]["evidence_unresolved"] == 1
        assert tables["jargons"]["reasons"]["missing_evidence"] == 1
        assert tables["expressions"]["reasons"]["prompt_example"] == 1
    assert staging.read_bytes() == staging_bytes
    assert paths.identity_file.read_bytes() == identity_bytes
    expressions = store.entries(LanguageKind.EXPRESSION)
    expression, missing_example = expressions
    assert expression.count == (13 if old_target else 12)
    assert expression.example == ("本地例句" if old_target else "来庆祝跑步成绩")
    assert (missing_example.text, missing_example.example, missing_example.count) == (
        "缺例表达",
        "",
        1,
    )
    assert missing_example.evidence_ids == ()
    assert json.loads(expression.source_refs[0]) == ["private-instance", "main", "expressions", [1]]
    for entry in store.entries(LanguageKind.JARGON):
        if entry.detail in {"证据未知", "原库无引用", "已认定非黑话", ""}:
            assert len(entry.source_refs) == 1
            assert entry.evidence_ids == ()
            if entry.detail == "证据未知":
                assert entry.count == (0 if old_target else 4)
        else:
            assert json.loads(entry.source_refs[1])[:3] == [
                "private-instance",
                "main",
                "mai_messages",
            ]
    block = store.jargon_reference(["go gg"], 700)
    assert block.index("等候开局") < block.index("普通提醒")

    with closing(sqlite3.connect(paths.database_file)) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT raw_values FROM language_import_rows WHERE source_key = ?",
                (expression.source_refs[0],),
            ).fetchone()[0]
        )
        raw_example, raw_evidence = connection.execute(
            """SELECT
                (SELECT json_extract(raw_values, '$.content_list') FROM language_import_rows
                 WHERE table_name = 'expressions' AND json_extract(raw_values, '$.id') = 3),
                (SELECT json_extract(raw_values, '$.evidence_messages') FROM language_import_rows
                 WHERE table_name = 'jargons' AND json_extract(raw_values, '$.id') = 3)
            """
        ).fetchone()
    assert raw_example == "[]"
    assert json.loads(raw_evidence) == [[{"platform": "mock", "message_id": "03"}]]
    assert (raw["checked"], raw["modified_by"], raw["create_time"], raw["last_active_time"]) == (
        1,
        "USER",
        "t0",
        "t1",
    )
    assert raw["style"] == "使用 白桦冲线"

    # 同实例的新快照走新的暂存库，已有运行目标显式拒绝覆盖。
    new_package, new_staging = tmp_path / "new.zip", tmp_path / "new-staging.db"
    create_source(source, "DELETE FROM expressions WHERE id = 1;")
    assert run_command("export", "main", source, new_package) == 0
    assert run_command("import", "main", new_package, new_staging) == 0
    capsys.readouterr()
    assert _publish(new_staging, paths.database_file) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "language_snapshot_conflict"}
    assert store.entries(LanguageKind.EXPRESSION) == expressions
    store.observe([observed], [])
    store.observe([observed], [])
    reused, _ = store.entries(LanguageKind.EXPRESSION)
    assert (reused.count, reused.evidence_ids, reused.source_refs) == (
        13,
        (99,),
        expression.source_refs,
    )

    paths.runtime_config_file.write_text(
        "[body]\nsleep_enabled = false\n[judge]\nenabled = false\n", encoding="utf-8"
    )
    requests: list[OrganRequest] = []
    bus = OrganBus()
    for organ in ("planner", "replyer"):
        bus.register(LanguageOrgan(organ, requests))

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    with runtime.open_core(paths, clock=ManualClock(NOW), to_console=False) as core:
        heart = core.heartbeat
        heart.attach_channel("mock_private", lambda _message: None)
        heart.wake_up()
        assert heart.submit(
            Perception(
                "mock_private",
                "local",
                "GG，今天跑步。",
                VisibilityScope.USER_ONLY,
                TrustLevel.VERIFIED_USER,
            )
        )
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        plan, reply = requests
        assert "GG：抵达终点" in plan.prompt
        assert "gg：等候开局" in plan.prompt
        assert "GG：证据未知" in plan.prompt
        assert "GG：原库无引用" in plan.prompt
        assert "GG：已认定非黑话" in plan.prompt
        assert "GG：已认定非黑话" in dialogue_text(reply)
        text = dialogue_text(reply)
        assert "当「庆祝跑步成绩」时，可用「白桦冲线」" in text
        assert "当「庆祝跑步成绩」时，可用「缺例表达」" in text
        assert "禁用表达" not in text
        await heart.shut_down()
    assert paths.identity_file.read_bytes() == identity_bytes
