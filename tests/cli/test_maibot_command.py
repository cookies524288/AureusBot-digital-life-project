"""MaiBot inspect 命令的测试，只使用临时目录中的合成数据库。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from aureus.cli.main import main


@pytest.mark.parametrize("initialize", [False, True])
def test_maibot_inspect_empty_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], initialize: bool
) -> None:
    source = tmp_path / "empty.sqlite"
    if initialize:
        with closing(sqlite3.connect(source)) as database:
            database.execute("VACUUM")
    else:
        source.touch()
    before = source.read_bytes()
    home = tmp_path / "uninitialized"

    assert main(["--home", str(home), "migrate-maibot", "inspect", str(source)]) == 0

    result = capsys.readouterr()
    assert json.loads(result.out) == {
        "user_version": 0,
        "schema_migration_version": None,
        "tables": [],
        "derived_objects": [],
    }
    assert result.err == ""
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    ("content", "diagnostic"),
    [(None, "源文件不存在"), (b"fake non-SQLite content", "已损坏或不是 SQLite 数据库")],
)
def test_maibot_inspect_invalid_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes | None, diagnostic: str
) -> None:
    source = tmp_path / "private-location.sqlite"
    if content is not None:
        source.write_bytes(content)

    assert main(["migrate-maibot", "inspect", str(source)]) == 1

    result = capsys.readouterr()
    assert result.out == ""
    assert diagnostic in result.err
    assert source.name not in result.err
    if content is None:
        assert list(tmp_path.iterdir()) == []
    else:
        assert list(tmp_path.iterdir()) == [source]
        assert source.read_bytes() == content


def test_maibot_inspect_requires_an_explicit_source(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["migrate-maibot", "inspect"])

    assert error.value.code == 2
    assert capsys.readouterr().out == ""


def test_maibot_inspect_corrupt_sqlite_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "corrupt.sqlite"
    with closing(sqlite3.connect(source)) as database:
        database.execute("CREATE TABLE expressions (content TEXT)")
    content = bytearray(source.read_bytes())
    # 数据库文件头占 100 字节，下一字节是 sqlite_schema 所在的 B-tree 页类型。
    content[100] = 0
    source.write_bytes(content)

    assert main(["migrate-maibot", "inspect", str(source)]) == 1

    result = capsys.readouterr()
    assert result.out == ""
    assert "SQLITE_CORRUPT" in result.err
    assert source.read_bytes() == content


def test_maibot_inspect_reports_metadata_without_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "模拟库 #% .sqlite"
    with closing(sqlite3.connect(source)) as database:
        database.executescript("""
            PRAGMA user_version = 45;
            CREATE TABLE expressions (scope TEXT, id INTEGER, PRIMARY KEY (scope, id))
                WITHOUT ROWID;
            CREATE TABLE jargons (id INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE person_info (id INTEGER PRIMARY KEY);
            CREATE TABLE mai_messages (id INTEGER PRIMARY KEY);
            CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY);
            CREATE TABLE high_frequency_terms (id INTEGER PRIMARY KEY);
            CREATE TABLE "扩展""表' 名" (
                scope TEXT NOT NULL, expression_id INTEGER,
                content TEXT DEFAULT 'private-default',
                summary INTEGER GENERATED ALWAYS AS (length(content)) STORED,
                FOREIGN KEY (scope, expression_id) REFERENCES expressions (scope, id)
                    ON DELETE CASCADE
            );
            CREATE INDEX extension_by_scope ON "扩展""表' 名" (scope);
            CREATE VIEW derived_view AS SELECT private_view_function() FROM missing_table;
            CREATE TRIGGER extension_insert AFTER INSERT ON "扩展""表' 名" BEGIN
                SELECT 'private-trigger-body';
            END;
            CREATE VIRTUAL TABLE search_index USING fts5(content);
            INSERT INTO expressions VALUES ('private-scope', 42);
            INSERT INTO "扩展""表' 名" (scope, expression_id, content)
                VALUES ('private-scope', 42, 'private-chat');
            INSERT INTO search_index VALUES ('private-index-content');
        """)
    before = source.read_bytes()
    monkeypatch.chdir(tmp_path)

    assert main(["migrate-maibot", "inspect", source.name]) == 0

    result = capsys.readouterr()
    assert result.err == ""
    assert "private" not in result.out
    assert "private_view_function" not in result.out
    report = json.loads(result.out)
    assert report["user_version"] == 45
    assert report["schema_migration_version"] is None
    tables = {table["name"]: table for table in report["tables"]}
    assert {name: (table["category"], table["row_count"]) for name, table in tables.items()} == {
        "expressions": ("known", 1),
        "jargons": ("known", 0),
        "person_info": ("known", 0),
        "mai_messages": ("known", 0),
        "chat_sessions": ("known", 0),
        "high_frequency_terms": ("known", 0),
        "扩展\"表' 名": ("unknown", 1),
    }
    assert tables["expressions"]["columns"] == [
        {"name": "scope", "type": "TEXT", "not_null": True, "primary_key": 1},
        {"name": "id", "type": "INTEGER", "not_null": True, "primary_key": 2},
    ]
    extension = tables["扩展\"表' 名"]
    assert extension["columns"] == [
        {"name": "scope", "type": "TEXT", "not_null": True, "primary_key": 0},
        {"name": "expression_id", "type": "INTEGER", "not_null": False, "primary_key": 0},
        {"name": "content", "type": "TEXT", "not_null": False, "primary_key": 0},
        {"name": "summary", "type": "INTEGER", "not_null": False, "primary_key": 0},
    ]
    keys = extension["foreign_keys"]
    assert [(key["id"], key["sequence"]) for key in keys] == [(0, 0), (0, 1)]
    assert [(key["column"], key["references_column"]) for key in keys] == [
        ("scope", "scope"),
        ("expression_id", "id"),
    ]
    assert all(
        key["references_table"] == "expressions" and key["on_delete"] == "CASCADE" for key in keys
    )
    derived = {item["name"]: item["type"] for item in report["derived_objects"]}
    assert derived["derived_view"] == "view"
    assert derived["extension_by_scope"] == "index"
    assert derived["extension_insert"] == "trigger"
    assert derived["search_index"] == "virtual"
    assert derived["search_index_data"] == "shadow"
    assert derived["sqlite_sequence"] == "internal"
    assert source.read_bytes() == before
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("valid_versions", [(1, 27), ()])
def test_maibot_inspect_reports_only_valid_memory_migration_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], valid_versions: tuple[int, ...]
) -> None:
    source = tmp_path / "memory.sqlite"
    with closing(sqlite3.connect(source)) as database:
        database.executescript("""
            CREATE TABLE paragraphs (id INTEGER PRIMARY KEY);
            CREATE TABLE episodes (id INTEGER PRIMARY KEY);
            CREATE TABLE fact_claims (id INTEGER PRIMARY KEY);
            CREATE TABLE fact_evidence (id INTEGER PRIMARY KEY);
            CREATE TABLE schema_migrations (version, applied_at TEXT);
            INSERT INTO schema_migrations (version, applied_at)
                VALUES ('999', 'private-timestamp'), (99.5, NULL), (X'FF', NULL),
                       (NULL, NULL), (-1, NULL), (0, NULL);
        """)
        database.executemany(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            [(version,) for version in valid_versions],
        )
        database.commit()

    assert main(["migrate-maibot", "inspect", str(source)]) == 0

    result = capsys.readouterr()
    assert result.err == ""
    assert "private-timestamp" not in result.out
    report = json.loads(result.out)
    assert report["user_version"] == 0
    assert report["schema_migration_version"] == max(valid_versions, default=None)
    assert {table["name"]: table["category"] for table in report["tables"]} == {
        "episodes": "known",
        "fact_claims": "known",
        "fact_evidence": "known",
        "paragraphs": "known",
        "schema_migrations": "known",
    }


@pytest.mark.parametrize(
    "schema",
    [
        "CREATE TABLE schema_migrations (version INTEGER);",
        """
        CREATE TABLE paragraphs (id INTEGER PRIMARY KEY);
        CREATE TABLE episodes (id INTEGER PRIMARY KEY);
        CREATE TABLE fact_claims (id INTEGER PRIMARY KEY);
        CREATE TABLE fact_evidence (id INTEGER PRIMARY KEY);
        CREATE TABLE schema_migrations (revision INTEGER);
        """,
        """
        CREATE TABLE paragraphs (id INTEGER PRIMARY KEY);
        CREATE TABLE episodes (id INTEGER PRIMARY KEY);
        CREATE TABLE fact_claims (id INTEGER PRIMARY KEY);
        CREATE TABLE fact_evidence (id INTEGER PRIMARY KEY);
        CREATE TABLE schema_migrations (id INTEGER, version INTEGER AS (id + 100) STORED);
        """,
        """
        CREATE TABLE paragraphs (id INTEGER PRIMARY KEY);
        CREATE TABLE episodes (id INTEGER PRIMARY KEY);
        CREATE TABLE fact_claims (id INTEGER PRIMARY KEY);
        CREATE TABLE fact_evidence (id INTEGER PRIMARY KEY);
        CREATE VIEW schema_migrations AS SELECT 99 AS version;
        """,
    ],
)
def test_maibot_inspect_does_not_read_migration_version_without_memory_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], schema: str
) -> None:
    source = tmp_path / "other.sqlite"
    with closing(sqlite3.connect(source)) as database:
        database.executescript(schema)
        if "CREATE TABLE schema_migrations" in schema:
            database.executescript("INSERT INTO schema_migrations VALUES (99);")

    assert main(["migrate-maibot", "inspect", str(source)]) == 0

    result = capsys.readouterr()
    assert result.err == ""
    report = json.loads(result.out)
    assert report["schema_migration_version"] is None
    assert all(
        table["category"] == "unknown"
        for table in report["tables"]
        if table["name"] == "schema_migrations"
    )


def test_maibot_inspect_reads_committed_wal_without_writing_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "active.sqlite"
    wal = source.with_name(source.name + "-wal")
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        before = source.read_bytes()
        writer.executescript("""
            PRAGMA user_version = 19;
            CREATE TABLE expressions (content TEXT);
            INSERT INTO expressions VALUES ('committed chat');
        """)
        wal_before = wal.read_bytes()
        writer.execute("INSERT INTO expressions VALUES ('uncommitted chat')")

        assert main(["migrate-maibot", "inspect", str(source)]) == 0

        result = capsys.readouterr()
        assert result.err == ""
        report = json.loads(result.out)
        assert report["user_version"] == 19
        assert report["tables"][0]["name"] == "expressions"
        assert report["tables"][0]["row_count"] == 1
        assert "chat" not in result.out
        assert source.read_bytes() == before
        assert wal.read_bytes() == wal_before


def test_maibot_inspect_keeps_one_snapshot_during_wal_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "snapshot.sqlite"
    with closing(sqlite3.connect(source)) as writer:
        writer.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA user_version = 4;
            CREATE TABLE expressions (content TEXT);
        """)
        connect = sqlite3.connect
        committed = False

        def commit_after_version(statement: str) -> None:
            nonlocal committed
            if statement == "PRAGMA main.table_list":
                writer.executescript("""
                    PRAGMA user_version = 5;
                    ALTER TABLE expressions ADD COLUMN scope TEXT;
                    INSERT INTO expressions VALUES ('new chat', 'new scope');
                """)
                committed = True

        def connect_with_trace(database: str, *, uri: bool, autocommit: bool) -> sqlite3.Connection:
            reader = connect(database, uri=uri, autocommit=autocommit)
            reader.set_trace_callback(commit_after_version)
            return reader

        monkeypatch.setattr(sqlite3, "connect", connect_with_trace)

        assert main(["migrate-maibot", "inspect", str(source)]) == 0

        result = capsys.readouterr()
        assert committed
        assert result.err == ""
        report = json.loads(result.out)
        assert report["user_version"] == 4
        assert report["tables"][0]["row_count"] == 0
        assert [column["name"] for column in report["tables"][0]["columns"]] == ["content"]
