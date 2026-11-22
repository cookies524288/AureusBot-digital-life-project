"""从正式 CLI 导出、暂存及发布合成长记忆。"""

import json
import sqlite3
from base64 import b64encode
from contextlib import closing
from datetime import UTC, datetime
from hashlib import md5, sha256
from pathlib import Path

import msgpack
import pytest

from aureus.cli.main import main
from aureus.common import ManualClock
from aureus.memory import MemoryIndex, MemorySource
from aureus.timeline import TimelineDatabase, TimelineQuery

NOW = datetime(2026, 11, 22, tzinfo=UTC)
ORIGINAL = b"synthetic image payload"
ATTACHMENT = b"synthetic attachment payload"
MEMORY_IMAGE = b"synthetic memory image payload"
MEMORY_DIGEST = sha256(MEMORY_IMAGE).hexdigest()
MEMORY_STORAGE_KEY = f"{MEMORY_DIGEST}.png"


def _source(role: str, table: str, *key: str | int) -> MemorySource:
    return MemorySource("fixture", role, table, key)


def _make_main(path: Path) -> None:
    digest = sha256(ORIGINAL).hexdigest()
    with closing(sqlite3.connect(path)) as database:
        database.executescript("""
            CREATE TABLE mai_messages (
                id INTEGER PRIMARY KEY, message_id TEXT, timestamp DATETIME,
                received_at DATETIME, platform TEXT,
                user_id TEXT, user_nickname TEXT, session_id TEXT, reply_to TEXT,
                raw_content BLOB, processed_plain_text TEXT
            );
            CREATE TABLE images (
                id INTEGER PRIMARY KEY, image_hash TEXT, full_path TEXT, description TEXT,
                no_file_flag BOOLEAN, is_banned BOOLEAN, record_time DATETIME
            );
            CREATE TABLE binary_data (id INTEGER PRIMARY KEY, data_hash TEXT, full_path TEXT);
        """)
        for number, session, user in [
            (1, "group", "a"),
            (2, "direct", "a"),
            (3, None, "a"),
            (4, "group", "b"),
        ]:
            raw = msgpack.packb(
                [
                    {"type": "text", "data": f"木卫二 合成原文 {number}"},
                    {"type": "image", "hash": digest, "data": "图"},
                    {"type": "file", "data": {"base64": b64encode(ATTACHMENT).decode()}},
                ]
            )
            database.execute(
                "INSERT INTO mai_messages VALUES (?, ?, ?, ?, 'qq', ?, '同名', ?, ?, ?, ?)",
                (
                    number,
                    str(number),
                    "2026-10-22 08:10:20.234567",
                    "2026-10-22 08:10:22.123456" if number == 1 else None,
                    user,
                    session,
                    "1" if number == 2 else None,
                    raw,
                    "用于处理的消息",
                ),
            )
        database.executemany(
            "INSERT INTO images VALUES (?, ?, ?, ?, ?, ?, '2026-10-22 08:11:02')",
            [
                (1, digest, "one.bin", "木卫二照片", 0, 0),
                (2, "marked-missing", "marked.bin", "木卫二仅描述", 1, 0),
                (3, "banned", "banned.bin", "木卫二禁用图片", 1, 1),
            ],
        )
        database.execute("INSERT INTO binary_data VALUES (1, ?, 'missing.bin')", (digest,))
        database.executemany(
            "INSERT INTO mai_messages VALUES (?, ?, '2026-10-22', NULL, 'qq', 'c', "
            "'合成人物', 'group', NULL, ?, ?)",
            [
                (5, "legacy-json", b'[{"type":"text","data":"legacy raw text"}]', "fallback"),
                (6, "broken-json", b'[{"type":', "broken JSON fallback"),
                (7, "legacy-media", b'[{"type":"text","data":" "}]', "media fallback"),
            ],
        )
        database.commit()


def _make_memory(path: Path) -> None:
    with closing(sqlite3.connect(path)) as database:
        database.executescript("""
            CREATE TABLE paragraphs (
                hash TEXT PRIMARY KEY, content TEXT NOT NULL, event_time REAL,
                event_time_start REAL, time_granularity TEXT, is_deleted INTEGER,
                expires_at REAL
            );
            INSERT INTO paragraphs VALUES
                ('p1', '木卫二现场资料', NULL, 1792627820.5, 'second', 0, NULL),
                ('p2', '木卫二已删除段落', NULL, NULL, NULL, 1, NULL);
            CREATE TABLE relations (
                hash TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
                object TEXT NOT NULL, source_paragraph TEXT, is_inactive BOOLEAN
            );
            INSERT INTO relations VALUES
                ('r1', '木卫二', '出现于', '现场资料', 'p1', 0),
                ('r2', '木卫二', '属于', '失活关联', 'p2', 1);
            CREATE TABLE paragraph_relations (
                paragraph_hash TEXT, relation_hash TEXT, PRIMARY KEY(paragraph_hash, relation_hash)
            );
            INSERT INTO paragraph_relations VALUES ('p1', 'r1'), ('p1', 'r2');
            CREATE TABLE episodes (
                episode_id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,
                event_time_start REAL, time_granularity TEXT, evidence_ids_json TEXT
            );
            INSERT INTO episodes VALUES
                ('e1', '木卫二', '观测经历', 1792627820.5, 'second', '["p1","p2"]'),
                ('e2', '木卫二', '仅存正文的经历', 1792627820.5, 'second', '["missing"]');
            CREATE TABLE episode_paragraphs (
                episode_id TEXT, paragraph_hash TEXT, position INTEGER DEFAULT 0,
                PRIMARY KEY (episode_id, paragraph_hash)
            );
            INSERT INTO episode_paragraphs VALUES ('e1', 'p1', 0), ('e1', 'missing', 1);
            CREATE TABLE fact_claims (
                claim_id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
                fact_key TEXT NOT NULL, value_text TEXT NOT NULL, polarity TEXT NOT NULL,
                status TEXT NOT NULL, first_observed_at REAL NOT NULL, valid_from REAL,
                valid_to REAL
            );
            INSERT INTO fact_claims VALUES
                ('f1', 'person', 'fake', '偏好', '木卫二有效', 'positive', 'active',
                    1792627820.5, 1792627820.5, NULL),
                ('f2', 'person', 'fake', '偏好', '木卫二替代', 'positive', 'superseded',
                    1792627820.5, NULL, NULL),
                ('f3', 'person', 'fake', '偏好', '木卫二过期', 'positive', 'active',
                    1, NULL, 2),
                ('f4', 'person', 'fake', '偏好', '木卫二矛盾', 'positive', 'conflicted',
                    1792627820.5, NULL, NULL);
            CREATE TABLE fact_evidence (
                claim_id TEXT, evidence_type TEXT, evidence_id TEXT, stance TEXT,
                weight REAL, observed_at REAL NOT NULL, metadata_json TEXT,
                PRIMARY KEY (claim_id, evidence_type, evidence_id, stance)
            );
            INSERT INTO fact_evidence VALUES
                ('f1', 'paragraph', 'p1', 'support', 1, 1792627820.5, '{}'),
                ('f1', 'paragraph', 'p2', 'support', 1, 1792627820.5, '{}'),
                ('f1', 'paragraph', 'missing', 'support', 1, 1792627820.5, '{}'),
                ('unknown', 'paragraph', 'p1', 'support', 1, 1792627820.5, '{}');
            CREATE TABLE image_assets (
                asset_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE,
                storage_key TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE image_occurrences (
                occurrence_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL,
                external_ref TEXT NOT NULL UNIQUE, source_kind TEXT NOT NULL,
                scope_type TEXT NOT NULL, chat_id TEXT, message_id TEXT,
                component_path TEXT, occurred_at REAL, status TEXT NOT NULL
            );
            CREATE TABLE image_observations (
                observation_id TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL, text TEXT NOT NULL,
                source_kind TEXT NOT NULL, confirm_status TEXT NOT NULL, evidence_json TEXT,
                supersedes_id TEXT, superseded_by TEXT, created_at REAL NOT NULL
            );
            CREATE TABLE image_memory_links (
                link_id TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL, target_type TEXT NOT NULL,
                target_id TEXT NOT NULL, link_kind TEXT NOT NULL, evidence_json TEXT,
                status TEXT NOT NULL, UNIQUE (occurrence_id, target_type, target_id)
            );
            INSERT INTO image_occurrences VALUES
                ('o1', 'asset-active', 'chat:group:1:1', 'chat', 'chat', 'group', '1', '1',
                    1792627820.5, 'active'),
                ('o2', 'asset-active', 'chat:group:4:1', 'chat', 'chat', 'group', '4', '1',
                    1792627821.5, 'deleted'),
                ('o3', 'asset-deleted', 'chat:direct:2:1', 'chat', 'chat', 'direct', '2', '1',
                    1792627822.5, 'active'),
                ('o4', 'asset-missing', 'chat:group:1:2', 'chat', 'chat', 'group', '1', '2',
                    1792627823.5, 'active');
            INSERT INTO image_observations VALUES
                ('i1', 'o1', '木卫二可用图片观察', 'model_description', 'unconfirmed',
                    '{}', 'i4', '', 1792627825.5),
                ('i2', 'o2', '木卫二已删除图片出现', 'model_description', 'unconfirmed',
                    '{}', '', '', 1792627825.5),
                ('i3', 'o3', '木卫二已删除图片资产', 'model_description', 'unconfirmed',
                    '{}', '', '', 1792627825.5),
                ('i4', 'o1', '木卫二已替代图片观察', 'model_description', 'unconfirmed',
                    '{}', '', 'i1', 1792627825.5),
                ('i5', 'orphan', '木卫二孤立图片观察', 'model_description', 'unconfirmed',
                    '{}', '', '', 1792627825.5),
                ('i6', 'o4', '木卫二缺少资产的图片观察', 'model_description', 'unconfirmed',
                    '{}', '', '', 1792627825.5);
            INSERT INTO image_memory_links VALUES
                ('l1', 'o1', 'paragraph', 'p1', 'evidence', '{}', 'active'),
                ('l2', 'o1', 'paragraph', 'p2', 'evidence', '{}', 'invalid'),
                ('l3', 'o1', 'relation', 'missing', 'evidence', '{}', 'active');
        """)
        database.executemany(
            "INSERT INTO image_assets VALUES (?, ?, ?, ?)",
            [
                ("asset-active", MEMORY_DIGEST, MEMORY_STORAGE_KEY, "active"),
                ("asset-deleted", sha256(b"deleted").hexdigest(), "missing.png", "deleted"),
            ],
        )
        database.commit()


def test_cli_import_publish_restart_and_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main_db, memory_db, stage = (tmp_path / name for name in ("main", "memory", "stage"))
    _make_main(main_db)
    _make_memory(memory_db)
    media = tmp_path / "media"
    media.mkdir()
    (media / "one.bin").write_bytes(ORIGINAL)
    memory_media = tmp_path / "memory-media"
    memory_media.mkdir()
    (memory_media / MEMORY_STORAGE_KEY).write_bytes(MEMORY_IMAGE)
    prefix = ["migrate-maibot"]
    for role, path in (("main", main_db), ("memory", memory_db)):
        package = tmp_path / (role + ".zip")
        options = ["--instance", "fixture", "--role", role]
        assert main([*prefix, "export", *options, str(path), str(package)]) == 0
        assert main([*prefix, "import", *options, str(package), str(stage)]) == 0
    capsys.readouterr()
    home = tmp_path / "target"
    command = [
        *prefix,
        "publish-memory",
        str(stage),
        "--instance",
        "fixture",
        "--source-timezone",
        "Asia/Shanghai",
        "--main-media-root",
        str(media),
        "--memory-media-root",
        str(memory_media),
        "--target-home",
        str(home),
    ]
    assert main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out) == report
    changed_zone = ["UTC" if item == "Asia/Shanghai" else item for item in command]
    assert main(changed_zone) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "memory_snapshot_conflict"}
    assert "同名" not in json.dumps(report, ensure_ascii=False) and "fixture" not in str(report)
    tables = {(row["role"], row["table"]): row for row in report["tables"]}
    assert tables["main", "mai_messages"]["published"] == 7
    assert tables["main", "mai_messages"]["attachment_invalid_message_components"] == 1
    assert tables["main", "binary_data"]["attachment_missing"] == 1
    assert tables["memory", "episodes"]["unresolved_links"] == 2
    assert tables["memory", "fact_claims"]["archive"] == 2
    assert tables["main", "images"]["attachment_archived"] == 1
    assert tables["main", "images"]["attachment_marked_missing"] == 2
    assert tables["memory", "image_assets"]["attachment_archived"] == 1
    assert tables["memory", "image_assets"]["attachment_missing"] == 1
    assert tables["memory", "image_observations"]["unresolved_links"] == 2
    assert tables["memory", "image_memory_links"]["unresolved_links"] == 1
    assert tables["memory", "fact_evidence"]["unresolved_links"] == 2
    assert all(row["source"] == row.get("published", 0) for row in report["tables"])
    stored = home / "data" / "memory" / "attachments"
    assert (stored / sha256(ORIGINAL).hexdigest()).read_bytes() == ORIGINAL
    assert (stored / sha256(ATTACHMENT).hexdigest()).read_bytes() == ATTACHMENT
    assert (stored / MEMORY_DIGEST).read_bytes() == MEMORY_IMAGE

    runtime = home / "data" / "aureus.db"
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(runtime, clock)
    timeline.open()
    index = MemoryIndex(runtime, timeline, clock)
    index.open()
    for number, expected in (
        (5, "legacy raw text"),
        (6, "broken JSON fallback"),
        (7, "media fallback"),
    ):
        message = index.get_history(_source("main", "mai_messages", number))
        assert message is not None and message.text == expected
        assert [hit.source for hit in index.search([expected], (), 1)] == [message.source]
        kinds = {link.kind for link in message.evidence}
        assert ("attachment_invalid_message_components" in kinds) == (number == 6)
    with closing(sqlite3.connect(stage)) as database:
        damaged = database.execute(
            "SELECT record FROM source_rows WHERE table_name='mai_messages' AND source_key='[6]'"
        ).fetchone()
        assert json.loads(damaged[0])["values"]["raw_content"] == {
            "blob_base64": b64encode(b'[{"type":').decode()
        }
    person_id = md5(b"qq_a", usedforsecurity=False).hexdigest()
    expected_time = datetime(2026, 10, 22, 0, 10, 20, 234567, tzinfo=UTC)
    filters = {"person_key": f"maibot:fixture:{person_id}", "since": expected_time}
    matches = index.search(["木卫二"], (), 10, **filters)
    assert [hit.source for hit in matches] == [
        _source("main", "mai_messages", i) for i in (1, 2, 3)
    ]
    assert matches[0].recorded_at == expected_time and matches[0].time_precision == "亚秒"
    assert matches[1].text == "木卫二 合成原文 2"
    assert any(link.source == _source("main", "images", 1) for link in matches[1].evidence)
    assert any(link.source == _source("main", "mai_messages", 1) for link in matches[1].evidence)
    message = index.get_history(_source("main", "mai_messages", 1))
    legacy_message = index.get_history(_source("main", "mai_messages", 2))
    assert message is not None and message.recorded_at == expected_time
    assert message.received_at == datetime(2026, 10, 22, 0, 10, 22, 123456, tzinfo=UTC)
    assert legacy_message is not None and legacy_message.received_at is None
    all_hits = {hit.text for hit in index.search(["木卫二"], (), 30)}
    assert {
        "木卫二可用图片观察",
        "木卫二孤立图片观察",
        "木卫二缺少资产的图片观察",
        "木卫二 仅存正文的经历",
        "木卫二仅描述",
        "木卫二照片",
        "木卫二 出现于 现场资料",
        "偏好：木卫二有效",
    } <= all_hits
    assert not any(
        marker in hit
        for marker in ("删除", "替代", "过期", "失活", "禁用", "矛盾")
        for hit in all_hits
    )
    assert tables["memory", "image_observations"]["archive"] == 3
    episode = index.get_history(_source("memory", "episodes", "e1"))
    assert episode is not None
    assert {(link.source, link.status) for link in episode.evidence} == {
        (_source("memory", "paragraphs", "p1"), None),
        (_source("memory", "paragraphs", "p2"), "deleted"),
        (None, None),
    }
    fact = index.get_history(_source("memory", "fact_claims", "f1"))
    assert fact is not None and fact.person_key == "maibot:fixture:fake"
    assert fact.evidence == episode.evidence
    paragraph = index.get_history(_source("memory", "paragraphs", "p1"))
    assert paragraph is not None and paragraph.recorded_at == episode.recorded_at
    assert {(link.reference, link.status) for link in paragraph.evidence} == {
        ("r1", None),
        ("r2", "inactive"),
    }
    observation = index.get_history(_source("memory", "image_observations", "i1"))
    assert observation is not None and observation.recorded_at == episode.recorded_at
    assert {link.source for link in observation.evidence} == {
        _source("memory", "image_occurrences", "o1"),
        _source("memory", "image_assets", "asset-active"),
    }
    for observation_id, expected in (
        ("i5", {("orphan", None)}),
        ("i6", {("o4", _source("memory", "image_occurrences", "o4")), ("asset-missing", None)}),
    ):
        orphan = index.get_history(_source("memory", "image_observations", observation_id))
        assert orphan is not None and orphan.status == "active"
        assert {(link.reference, link.source) for link in orphan.evidence} == expected
    occurrence = index.get_history(_source("memory", "image_occurrences", "o1"))
    assert occurrence is not None
    assert {(link.kind, link.reference, link.source) for link in occurrence.evidence} == {
        ("image_assets", "asset-active", _source("memory", "image_assets", "asset-active")),
        ("message", "1", _source("main", "mai_messages", 1)),
        ("paragraph", "p1", _source("memory", "paragraphs", "p1")),
        ("relation", "missing", None),
    }
    assert not timeline.query(TimelineQuery())
    index.close()
    with closing(sqlite3.connect(runtime)) as database:
        database.execute("DROP TABLE memory_history_fts")
    index.open()
    assert index.search(["木卫二"], (), 10, **filters) == matches
    index.close()
    timeline.close()
