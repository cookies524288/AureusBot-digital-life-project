"""MaiBot 迁移工具：只读盘点 SQLite 结构与记录数（路线图 3.1）。"""

import sqlite3
from contextlib import closing
from pathlib import Path

# MaiBot 主库与 A_Memorix 的已知表，见 13 号文档第 2.2、2.5 节。
_MAIN_TABLES = frozenset(
    {
        "expressions",
        "jargons",
        "person_info",
        "mai_messages",
        "chat_sessions",
        "high_frequency_terms",
    }
)
_MEMORY_TABLES = frozenset({"paragraphs", "episodes", "fact_claims", "fact_evidence"})
_MIGRATION_TABLE = "schema_migrations"


def _inspect_table(
    database: sqlite3.Connection, name: str, known_tables: frozenset[str]
) -> dict[str, object]:
    # PRAGMA 的参数不能绑定。表名在 PRAGMA 中按字符串转义，在 FROM 中按标识符转义。
    pragma_name = "'" + name.replace("'", "''") + "'"
    identifier = quote_identifier(name)
    columns = [
        {"name": column, "type": kind, "not_null": bool(not_null), "primary_key": primary_key}
        for _, column, kind, not_null, _, primary_key, _ in database.execute(
            f"PRAGMA main.table_xinfo({pragma_name})"
        )
    ]
    foreign_keys = [
        {
            "id": key_id,
            "sequence": sequence,
            "column": column,
            "references_table": table,
            "references_column": target,
            "on_update": on_update,
            "on_delete": on_delete,
        }
        for key_id, sequence, table, column, target, on_update, on_delete, _ in database.execute(
            f"PRAGMA main.foreign_key_list({pragma_name})"
        )
    ]
    count = database.execute(f"SELECT COUNT(*) FROM main.{identifier}").fetchone()[0]
    return {
        "name": name,
        "category": "known" if name.casefold() in known_tables else "unknown",
        "columns": columns,
        "foreign_keys": foreign_keys,
        "row_count": count,
    }


def _has_memory_migrations(database: sqlite3.Connection, table_names: set[str]) -> bool:
    # 许多应用都使用 schema_migrations 这个名字。以记忆业务表和存储字段共同确认来源。
    return (_MEMORY_TABLES | {_MIGRATION_TABLE}) <= table_names and any(
        name.casefold() == "version" and hidden == 0
        for _, name, _, _, _, _, hidden in database.execute(
            "PRAGMA main.table_xinfo('schema_migrations')"
        )
    )


def _inspect_schema(database: sqlite3.Connection) -> dict[str, object]:
    user_version = database.execute("PRAGMA main.user_version").fetchone()[0]
    objects = read_schema_objects(database)
    table_names = sorted(name for name, kind in objects.items() if kind == "table")
    known_tables = _MAIN_TABLES | _MEMORY_TABLES
    migration_version = None
    if _has_memory_migrations(database, {name.casefold() for name in table_names}):
        known_tables |= {_MIGRATION_TABLE}
        migration_version = database.execute(
            "SELECT MAX(version) FROM main.schema_migrations "
            "WHERE typeof(version) = 'integer' AND version > 0"
        ).fetchone()[0]

    derived = [(name, kind) for name, kind in objects.items() if kind != "table"]
    derived.extend(
        database.execute(
            "SELECT name, type FROM main.sqlite_schema WHERE type IN ('index', 'trigger')"
        )
    )
    return {
        "user_version": user_version,
        "schema_migration_version": migration_version,
        "tables": [_inspect_table(database, name, known_tables) for name in table_names],
        "derived_objects": [{"name": name, "type": kind} for name, kind in sorted(derived)],
    }


def quote_identifier(name: str) -> str:
    """为 SQLite 的表或列名加引号。"""
    return '"' + name.replace('"', '""') + '"'


def read_schema_objects(database: sqlite3.Connection) -> dict[str, str]:
    """区分普通表、内部表、虚拟表、影子表与视图。"""
    return {
        name: "internal" if kind == "table" and name.casefold().startswith("sqlite_") else kind
        for schema, name, kind, *_ in database.execute("PRAGMA main.table_list")
        if schema == "main" and name != "sqlite_schema"
    }


def open_readonly_database(source: Path) -> sqlite3.Connection:
    """打开显式指定的 SQLite 文件，调用方负责关闭连接。"""
    source.stat()
    # mode=ro 保留 SQLite 的 WAL 读取机制，能看见尚未写回主文件的提交。
    uri = f"{source.absolute().as_uri()}?mode=ro"
    database = sqlite3.connect(uri, uri=True, autocommit=True)
    # 禁止执行库内声明的视图及扩展，并限制不受信任的模式使用 SQL 函数。
    database.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_VIEW, False)
    database.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False)
    database.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
    return database


def inspect_database(source: Path) -> dict[str, object]:
    """从指定源库读取同一快照的版本、结构、键与计数。

    Args:
        source: 调用方明确指定的 SQLite 文件。

    Returns:
        可序列化为 JSON 的盘点结果。

    Raises:
        OSError: 源文件不存在或无法访问。
        sqlite3.Error: 无法打开或读取源库。
    """
    with closing(open_readonly_database(source)) as database:
        # 关闭连接时结束只读事务，版本、结构和计数始终来自同一快照。
        database.execute("BEGIN")
        return _inspect_schema(database)
