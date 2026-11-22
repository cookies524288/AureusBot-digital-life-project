"""Phase 3.2 的离线 CLI、快照及对账验证。"""

import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from aureus.cli.main import main
from aureus.tools.migrate import maibot_package

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
            platform TEXT, id, speaker TEXT, sent REAL, payload BLOB, full_path TEXT,
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
    assert rows[0]["values"]["payload"] == {"blob_base64": "AH8="}
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
