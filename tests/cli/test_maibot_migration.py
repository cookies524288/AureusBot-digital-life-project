"""Phase 3.2 的离线 CLI、快照及对账验证。"""

import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from aureus.cli.main import main
from aureus.tools.migrate import maibot_package, maibot_staging

SOURCE_ARGS = ["--instance", "private-instance", "--role"]


def create_source(path: Path, schema: str) -> None:
    with closing(sqlite3.connect(path)) as database:
        database.executescript(schema)


def run_command(operation: str, role: str, *paths: Path) -> int:
    return main(["migrate-maibot", operation, *SOURCE_ARGS, role, *map(str, paths)])


def read_package(path: Path) -> tuple[dict, list[dict]]:
    with zipfile.ZipFile(path) as archive:
        return (
            json.loads(archive.read("manifest.json")),
            [json.loads(line) for line in archive.read("records.jsonl").splitlines()],
        )


def test_export_and_preview_keep_typed_keys_and_private_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "private-name.sqlite"
    package = tmp_path / "private-package.zip"
    create_source(
        source,
        """
        PRAGMA user_version = 45;
        CREATE TABLE mai_messages (
            platform TEXT, id, speaker TEXT, sent REAL, raw_content BLOB, full_path TEXT,
            PRIMARY KEY (id, platform)
        );
        INSERT INTO mai_messages VALUES ('qq', 1, 'private-speaker', 123.456789, x'007F', 'x/y');
        INSERT INTO mai_messages VALUES ('qq', '1', NULL, NULL, NULL, NULL);
        INSERT INTO mai_messages VALUES ('qq', NULL, 'isolated', NULL, NULL, NULL);
        CREATE TABLE expressions (content TEXT);
        INSERT INTO expressions VALUES ('private-example');
        CREATE TABLE "private-unknown" (id INTEGER PRIMARY KEY, content);
        INSERT INTO "private-unknown" VALUES (1, 'private-unknown-value');
        CREATE VIEW jargons AS SELECT * FROM expressions;
    """,
    )
    original = source.read_bytes()
    assert run_command("export", "main", source, package) == 0
    output = capsys.readouterr().out
    assert "private" not in output
    report = json.loads(output)
    assert report["user_version"] == 45
    assert [(item["reason"], item["source"]) for item in report["tables"]] == [
        ("no_primary_key", 1),
        ("recognized", 3),
        ("unknown_table", 1),
    ]
    assert report["tables"][1]["quarantined"] == 1
    manifest, rows = read_package(package)
    assert manifest["snapshot_scope"] == "single_database_at_read_transaction_start"
    assert [row["key"] for row in rows] == [[1, "qq"], ["1", "qq"]]
    assert rows[0]["values"]["raw_content"] == {"blob_base64": "AH8="}
    assert rows[0]["deferred_media_fields"] == ["raw_content"]
    assert rows[0]["values"]["sent"] == 123.456789
    assert rows[0]["external_refs"] == {"full_path": "x/y"}
    assert rows[1]["values"]["speaker"] is None
    assert source.read_bytes() == original
    source.unlink()
    assert run_command("preview", "main", package) == 0
    assert {file.name for file in tmp_path.iterdir()} == {package.name}


def test_backup_freezes_committed_wal_and_preserves_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live.sqlite"
    package = tmp_path / "backup.zip"
    with closing(sqlite3.connect(source, autocommit=True)) as writer:
        writer.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA wal_autocheckpoint = 0;
            PRAGMA user_version = 45;
            CREATE TABLE mai_messages (id INTEGER PRIMARY KEY, text TEXT);
            INSERT INTO mai_messages VALUES (1, 'first');
        """)
        before = source.read_bytes()
        wal = source.with_name("live.sqlite-wal")
        assert wal.stat().st_size > 0
        open_readonly = maibot_package.open_readonly_database
        written = False

        def connect_with_update(path: Path) -> sqlite3.Connection:
            database = open_readonly(path)

            def after_first_backup_page(_status: int, _remaining: int, _total: int) -> None:
                nonlocal written
                if not written:
                    writer.executescript("""
                        PRAGMA user_version = 46;
                        ALTER TABLE mai_messages ADD COLUMN new_field;
                        INSERT INTO mai_messages VALUES (2, 'concurrent', 'new');
                    """)
                    written = True

            if path == source:

                class SnapshotReader(sqlite3.Connection):
                    def backup(self, target: sqlite3.Connection, **_options: object) -> None:
                        super().backup(target, pages=1, progress=after_first_backup_page)

                database.close()
                database = sqlite3.connect(
                    f"{path.as_uri()}?mode=ro", uri=True, autocommit=True, factory=SnapshotReader
                )
            return database

        monkeypatch.setattr(maibot_package, "open_readonly_database", connect_with_update)
        assert run_command("export", "main", source, package) == 0
        assert written
        assert source.read_bytes() == before
        assert writer.execute("SELECT COUNT(*) FROM mai_messages").fetchone() == (2,)
    assert json.loads(capsys.readouterr().out)["user_version"] == 45
    manifest, records = read_package(package)
    assert manifest["user_version"] == 45
    assert records[0]["values"] == {"id": 1, "text": "first"}
    assert len(records) == 1
    with zipfile.ZipFile(package) as archive:
        backup = archive.read("source.sqlite3")
    assert hashlib.sha256(backup).hexdigest() == manifest["snapshot_sha256"]
    restored = tmp_path / "restored.sqlite"
    restored.write_bytes(backup)
    with closing(sqlite3.connect(restored)) as database:
        assert database.execute("SELECT * FROM mai_messages").fetchall() == [(1, "first")]
        assert database.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_export_failure_does_not_overwrite_or_publish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "private-source.sqlite"
    package = tmp_path / "private-package.zip"
    source.write_bytes(b"private-invalid-file")
    assert run_command("export", "main", source, package) == 1
    assert not package.exists()
    package.write_bytes(b"existing")
    assert run_command("export", "main", source, package) == 1
    assert package.read_bytes() == b"existing"
    package.unlink()
    source.unlink()
    create_source(source, "CREATE TABLE mai_messages (id INTEGER PRIMARY KEY);")

    def interrupt(_database: Path, _snapshot: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(maibot_package, "_backup", interrupt)
    assert run_command("export", "main", source, package) == 130
    assert not package.exists()
    assert "private" not in capsys.readouterr().err


def test_cli_imports_all_roles_and_recovers_independent_snapshots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "private-staging.sqlite"
    schemas = {
        "main": """
            CREATE TABLE mai_messages (
                id INTEGER PRIMARY KEY, session_id, platform, user_id, timestamp, reply_to,
                processed_plain_text, group_id
            );
            INSERT INTO mai_messages VALUES
                (1, 'group', 'qq', 'synthetic-speaker', '2026-11-21 10:00:00.123456',
                 'evidence-a', 'fake group chat', 'synthetic-group'),
                (2, 'private', 'qq', 'synthetic-speaker', '2026-11-21 11:00:00.000001',
                 'evidence-b', 'fake private chat', NULL),
                (3, NULL, 'qq', 'synthetic-speaker', '2026-11-21 12:00:00.000009',
                 'evidence-c', 'fake without session', NULL),
                (4, NULL, NULL, NULL, NULL, NULL, 'fake missing values', NULL);
            CREATE TABLE images (id INTEGER PRIMARY KEY, full_path, disabled);
            INSERT INTO images VALUES (1, 'private-file.png', 1);
            CREATE TABLE expressions (content);
            INSERT INTO expressions VALUES ('fake unkeyed expression');
            CREATE TABLE "private-plugin" (data);
            INSERT INTO "private-plugin" VALUES ('fake unknown data');
        """,
        "memory": """
            CREATE TABLE paragraphs (id INTEGER PRIMARY KEY, content, timestamp, deleted);
            INSERT INTO paragraphs VALUES (1, 'fake paragraph', 123.456789, 1);
            CREATE TABLE fact_evidence (
                paragraph_id INTEGER, message_id TEXT, metadata_json TEXT,
                PRIMARY KEY (message_id, paragraph_id)
            );
            INSERT INTO fact_evidence VALUES (1, '2', '{"private-evidence": ["a", 3]}');
            CREATE TABLE image_assets (asset_id TEXT PRIMARY KEY, storage_key);
            INSERT INTO image_assets VALUES ('abc', 'private-asset-key');
            CREATE TABLE person_profile_alias_overrides (id INTEGER PRIMARY KEY, alias);
            INSERT INTO person_profile_alias_overrides VALUES (1, 'private-manual-alias');
            CREATE TABLE image_embeddings (id INTEGER PRIMARY KEY, embedding);
            INSERT INTO image_embeddings VALUES (1, x'0123');
        """,
        "people": """
            CREATE TABLE person_state (id INTEGER PRIMARY KEY, platform, user_id);
            INSERT INTO person_state VALUES (1, 'qq', 'synthetic-speaker');
            CREATE TABLE relation_edge (id INTEGER PRIMARY KEY, person_id, evidence);
            INSERT INTO relation_edge VALUES (1, 1, 'evidence-c');
            CREATE TABLE thread_item (id INTEGER PRIMARY KEY, person_id);
            INSERT INTO thread_item VALUES (1, 1);
            CREATE TABLE events (id INTEGER PRIMARY KEY, event_time);
            INSERT INTO events VALUES (1, 123.456);
            CREATE TABLE daily_schedule (id INTEGER PRIMARY KEY, event_id);
            INSERT INTO daily_schedule VALUES (1, 1);
        """,
    }
    for role, schema in schemas.items():
        source = tmp_path / f"private-{role}.sqlite"
        package = tmp_path / f"private-{role}.zip"
        create_source(source, schema)
        assert run_command("export", role, source, package) == 0
        source.unlink()
        assert run_command("preview", role, package) == 0
        assert run_command("import", role, package, target) == 0
        assert run_command("import", role, package, target) == 0
        output = capsys.readouterr()
        assert output.err == ""
        assert all(value not in output.out for value in ("private-", "fake", "synthetic-"))
        decoder = json.JSONDecoder()
        reports = []
        remaining = output.out
        while remaining.strip():
            report, end = decoder.raw_decode(remaining.lstrip())
            reports.append(report)
            remaining = remaining.lstrip()[end:]
        first_import, repeated_import = reports[-2:]
        for first, repeated in zip(first_import["tables"], repeated_import["tables"], strict=True):
            assert first["source"] == first["received"] + first["quarantined"] + first["skipped"]
            assert repeated["received"] == 0
            assert repeated["already_exists"] == first["received"]
        package.unlink()
    with closing(sqlite3.connect(target)) as database:
        experiences = database.execute("""
            SELECT record FROM source_rows
            WHERE table_name = 'mai_messages'
                AND json_extract(record, '$.values.user_id') = 'synthetic-speaker'
            ORDER BY source_key
        """).fetchall()
        rows = [json.loads(record)["values"] for (record,) in experiences]
        assert [row["session_id"] for row in rows] == ["group", "private", None]
        assert [row["reply_to"] for row in rows] == ["evidence-a", "evidence-b", "evidence-c"]
        assert [row["timestamp"][-6:] for row in rows] == ["123456", "000001", "000009"]
        assert [row["group_id"] for row in rows] == ["synthetic-group", None, None]
        assert {row["platform"] for row in rows} == {"qq"}
        assert database.execute("SELECT COUNT(*) FROM sources").fetchone() == (3,)
        assert database.execute(
            "SELECT COUNT(*) FROM source_rows WHERE source_key = '[1]'"
        ).fetchone() == (8,)
        memory = database.execute(
            "SELECT table_name, record FROM source_rows WHERE table_name IN "
            "('paragraphs', 'fact_evidence', 'image_assets', 'person_profile_alias_overrides')"
        )
        records = {table: json.loads(record) for table, record in memory}
        assert records["paragraphs"]["values"]["deleted"] == 1
        assert records["fact_evidence"]["key"] == ["2", 1]
        assert (
            records["fact_evidence"]["values"]["metadata_json"] == '{"private-evidence": ["a", 3]}'
        )
        assert records["image_assets"]["external_refs"] == {"storage_key": "private-asset-key"}
        assert "person_profile_alias_overrides" not in records
    restored = tmp_path / "private-restored.sqlite"
    assert run_command("restore", "main", target, restored) == 0
    assert run_command("restore", "main", target, restored) == 1
    with closing(sqlite3.connect(restored)) as database:
        assert database.execute('SELECT data FROM "private-plugin"').fetchone() == (
            "fake unknown data",
        )
        assert database.execute("SELECT content FROM expressions").fetchone() == (
            "fake unkeyed expression",
        )
        assert database.execute("SELECT COUNT(*) FROM mai_messages").fetchone() == (4,)
        assert database.execute(
            "SELECT platform, user_id, timestamp, reply_to FROM mai_messages WHERE id = 4"
        ).fetchone() == (None, None, None, None)
        assert database.execute("SELECT full_path, disabled FROM images").fetchone() == (
            "private-file.png",
            1,
        )
    for role, table, expected in (
        ("memory", "fact_evidence", (1, "2", '{"private-evidence": ["a", 3]}')),
        ("people", "relation_edge", (1, 1, "evidence-c")),
    ):
        other = tmp_path / f"restored-{role}.sqlite"
        assert run_command("restore", role, target, other) == 0
        with closing(sqlite3.connect(other)) as database:
            assert database.execute(f"SELECT * FROM {table}").fetchone() == expected


@pytest.mark.parametrize(
    "damage", ["version", "duplicate", "truncated", "snapshot", "count", "zip"]
)
def test_preview_and_import_reject_damaged_packages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    source = tmp_path / "source.sqlite"
    package = tmp_path / "package.zip"
    damaged = tmp_path / "damaged.zip"
    target = tmp_path / "target.sqlite"
    create_source(
        source,
        """
        CREATE TABLE mai_messages (id INTEGER PRIMARY KEY, text);
        INSERT INTO mai_messages VALUES (1, 'private-a'), (2, 'private-b');
    """,
    )
    assert run_command("export", "main", source, package) == 0
    capsys.readouterr()
    with zipfile.ZipFile(package) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    if damage == "version":
        manifest["version"] += 1
    if damage == "count":
        manifest["tables"][0]["source"] += 1
    if damage == "duplicate":
        first = files["records.jsonl"].splitlines(keepends=True)[0]
        files["records.jsonl"] = first + first
    if damage == "truncated":
        files["records.jsonl"] = files["records.jsonl"][:-2]
    if damage == "snapshot":
        files["source.sqlite3"] = files["source.sqlite3"][:-1]
    if damage in ("duplicate", "truncated"):
        manifest["records_sha256"] = hashlib.sha256(files["records.jsonl"]).hexdigest()
    files["manifest.json"] = maibot_package.encode_json(manifest)
    with zipfile.ZipFile(damaged, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    if damage == "zip":
        damaged.write_bytes(damaged.read_bytes()[:-70])
    for operation, paths in (("preview", (damaged,)), ("import", (damaged, target))):
        assert run_command(operation, "main", *paths) == 1
        output = capsys.readouterr()
        assert output.out == ""
        assert "private" not in output.err
        error = json.loads(output.err)
        if damage in ("duplicate", "truncated"):
            assert error == {"error": "record_mismatch", "table": 1, "row": 2}
    assert not target.exists()
    assert run_command("import", "main", package, target) == 0


def test_import_rolls_back_flushed_rows_and_rejects_snapshot_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.sqlite"
    package = tmp_path / "package.zip"
    updated = tmp_path / "updated.zip"
    people = tmp_path / "people.zip"
    target = tmp_path / "target.sqlite"
    create_source(
        source,
        """
        CREATE TABLE mai_messages (id INTEGER PRIMARY KEY, text, deleted);
        INSERT INTO mai_messages VALUES (1, 'private-value', 0);
        CREATE TABLE person_state (id INTEGER PRIMARY KEY);
        INSERT INTO person_state VALUES (1), (2);
    """,
    )
    original = source.read_bytes()
    assert run_command("export", "main", source, package) == 0
    assert run_command("export", "people", source, people) == 0
    assert run_command("import", "main", package, source) == 1
    assert source.read_bytes() == original
    assert run_command("import", "main", package, target) == 0
    target_before = target.read_bytes()
    write_snapshot = maibot_staging._write_snapshot

    def interrupt(database: sqlite3.Connection, source_id: int, snapshot: Path) -> None:
        # 业务行与备份块都已写入事务后，再模拟进程收到停止信号。
        write_snapshot(database, source_id, snapshot)
        assert database.execute("SELECT COUNT(*) FROM source_rows").fetchone() == (3,)
        raise KeyboardInterrupt

    monkeypatch.setattr(maibot_staging, "_write_snapshot", interrupt)
    assert run_command("import", "people", people, target) == 130
    assert target.read_bytes() == target_before
    monkeypatch.setattr(maibot_staging, "_write_snapshot", write_snapshot)
    assert run_command("import", "people", people, target) == 0
    create_source(source, "PRAGMA user_version = 2; UPDATE mai_messages SET deleted = 1;")
    assert run_command("export", "main", source, updated) == 0
    target_before = target.read_bytes()
    capsys.readouterr()
    assert run_command("import", "main", updated, target) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "snapshot_conflict"}
    assert target.read_bytes() == target_before
    assert run_command("import", "main", package, target) == 0
    new_target = tmp_path / "new-target.sqlite"
    assert run_command("import", "main", updated, new_target) == 0
    assert run_command("import", "main", package, new_target) == 1
    with closing(sqlite3.connect(new_target)) as database:
        value = database.execute(
            "SELECT json_extract(record, '$.values.deleted') FROM source_rows"
        ).fetchone()
        assert value == (1,)


def test_migration_help_lists_order_and_parameters(capsys: pytest.CaptureFixture[str]) -> None:
    for prefix, expected in (
        ([], ["inspect", "export", "preview", "import", "restore"]),
        (["export"], ["--instance", "--role", "main", "memory", "people"]),
    ):
        with pytest.raises(SystemExit) as exit_info:
            main(["migrate-maibot", *prefix, "--help"])
        assert exit_info.value.code == 0
        output = capsys.readouterr().out
        assert all(word in output for word in expected)
        assert "private" not in output


def test_snapshot_restore_streams_multiple_blocks_and_detects_damage(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    package = tmp_path / "package.zip"
    target = tmp_path / "target.sqlite"
    output = tmp_path / "restored.sqlite"
    create_source(source, "CREATE TABLE unknown_data (data BLOB);")
    size = maibot_package.BLOCK_SIZE + 13
    with closing(sqlite3.connect(source)) as database, database:
        database.execute("INSERT INTO unknown_data VALUES (zeroblob(?))", (size,))
    assert run_command("export", "main", source, package) == 0
    assert run_command("import", "main", package, target) == 0
    source.unlink()
    package.unlink()
    assert run_command("restore", "main", target, output) == 0
    with closing(sqlite3.connect(output)) as database:
        assert database.execute("SELECT length(data) FROM unknown_data").fetchone() == (size,)
    with closing(sqlite3.connect(target)) as database, database:
        assert database.execute("SELECT count(*) FROM snapshot_blocks").fetchone() == (2,)
        database.execute("DELETE FROM snapshot_blocks WHERE block_number = 1")
    output.unlink()
    assert run_command("restore", "main", target, output) == 1
    assert not output.exists()
