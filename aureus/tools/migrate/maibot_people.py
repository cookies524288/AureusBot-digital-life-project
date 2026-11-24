"""3.3 将已暂存的 MaiBot 人际资料发布到人际子系统。"""

import json
import sqlite3
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from pydantic import JsonValue

from aureus.common import AureusPaths, Clock, ConfigError
from aureus.core.runtime import open_people
from aureus.identity import IdentityError, IdentityService, SingleInstanceLockError
from aureus.people import HistoricalKind, HistoricalPublication, HistoricalRecord, PeopleError
from aureus.tools.migrate.maibot_catalog import MigrationError, encode_row, read_rows, read_tables
from aureus.tools.migrate.maibot_package import Source
from aureus.tools.migrate.maibot_staging import open_staged_snapshot, open_staging

_TABLES = {
    HistoricalKind.PROFILE: "main",
    HistoricalKind.STATE: "people",
    HistoricalKind.RELATION: "people",
    HistoricalKind.EVENT: "people",
    HistoricalKind.THREAD_ITEM: "people",
    HistoricalKind.PROFILE_SNAPSHOT: "memory",
    HistoricalKind.PROFILE_OVERRIDE: "memory",
    HistoricalKind.ALIAS_OVERRIDE: "memory",
}
_STATE_COLUMNS = ("affinity", "mood", "ignore", "thread")
_MANUAL_KINDS = {HistoricalKind.PROFILE_OVERRIDE, HistoricalKind.ALIAS_OVERRIDE}


def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _identifier(value: JsonValue) -> str | None:
    if type(value) is int:
        return str(value)
    return _text(value)


def _aliases(value: JsonValue) -> list[str] | None:
    if not isinstance(value, str):
        return None
    try:
        items = json.loads(value)
    except ValueError:
        return None
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        return None
    return items


def _record(
    instance: str, kind: HistoricalKind, source_key: str, values: dict[str, JsonValue]
) -> HistoricalRecord:
    record = HistoricalRecord(
        instance=instance,
        kind=kind,
        source_key=source_key,
        subject_key=_identifier(values.get("person_id")),
        original=values,
    )
    match kind:
        case HistoricalKind.PROFILE:
            platform = _text(values.get("platform"))
            account = _identifier(values.get("user_id"))
            nickname = _text(values.get("user_nickname"))
            updates = {
                "account": (platform, account) if platform and account else None,
                "name": _text(values.get("person_name")) or nickname,
                "nickname": nickname,
                "group_cardname": values.get("group_cardname"),
                "name_reason": _text(values.get("name_reason")),
                "memory_points": values.get("memory_points"),
                "known_since": values.get("first_known_time"),
            }
        case HistoricalKind.STATE:
            updates = {
                "name": _text(values.get("display_name")),
                "relationship_state": {key: values[key] for key in _STATE_COLUMNS if key in values},
                "occurred_at": values.get("updated_at"),
            }
        case HistoricalKind.RELATION:
            weight = values.get("weight")
            updates = {
                "subject_key": _identifier(values.get("from_person")),
                "other_key": _identifier(values.get("to_person")),
                "relation_type": _text(values.get("kind")),
                "weight": weight if type(weight) in (int, float) else None,
                "evidence": values.get("evidence"),
                "occurred_at": values.get("updated_at"),
            }
        case HistoricalKind.EVENT:
            updates = {
                "event_type": _text(values.get("kind")),
                "occurred_at": values.get("ts"),
                "summary": _text(values.get("detail")),
            }
        case HistoricalKind.THREAD_ITEM:
            updates = {
                "summary": _text(values.get("content")),
                "occurred_at": values.get("created_at"),
                "due_at": values.get("due_at"),
                "hardness": values.get("hardness"),
            }
        case HistoricalKind.PROFILE_SNAPSHOT:
            version = values.get("profile_version")
            updates = {
                "profile_version": version if type(version) is int else None,
                "summary": _text(values.get("profile_text")),
                "aliases": _aliases(values.get("aliases_json")),
                "occurred_at": values.get("updated_at"),
                "expires_at": values.get("expires_at"),
            }
        case HistoricalKind.PROFILE_OVERRIDE:
            updates = {
                "summary": _text(values.get("override_text")),
                "occurred_at": values.get("updated_at"),
            }
        case HistoricalKind.ALIAS_OVERRIDE:
            updates = {
                "aliases": _aliases(values.get("aliases_json")),
                "occurred_at": values.get("updated_at"),
            }
        case HistoricalKind.LOCAL_ALIAS_OVERRIDE:
            raise MigrationError("invalid_staged_people_record")
    return record.model_copy(update=updates)


def _decode_record(
    instance: str, kind: HistoricalKind, source_key: str, encoded: str | bytes
) -> HistoricalRecord:
    try:
        values = json.loads(encoded)["values"]
    except (ValueError, TypeError, KeyError):
        raise MigrationError("invalid_staged_people_record") from None
    if not isinstance(values, dict):
        raise MigrationError("invalid_staged_people_record")
    return _record(instance, kind, source_key, values)


def _read_snapshot_profiles(
    database: sqlite3.Connection, instance: str, missing: set[HistoricalKind], work_parent: Path
) -> list[HistoricalRecord]:
    records: list[HistoricalRecord] = []
    with open_staged_snapshot(Source(instance, "memory"), database, work_parent) as snapshot:
        for table in read_tables(snapshot, "memory"):
            if table.name.casefold() not in missing:
                continue
            if not table.primary_key:
                raise MigrationError("invalid_people_override_key")
            kind = HistoricalKind(table.name.casefold())
            for key, values in read_rows(snapshot, table):
                encoded_key, encoded = encode_row(table, key, values)
                records.append(_decode_record(instance, kind, encoded_key.decode("ascii"), encoded))
    return records


def _read_records(staging: Path, instance: str, work_parent: Path) -> list[HistoricalRecord]:
    records: list[HistoricalRecord] = []
    with open_staging(staging) as database:
        roles = {
            row[0]
            for row in database.execute("SELECT role FROM sources WHERE instance = ?", (instance,))
        }
        if not roles:
            raise MigrationError("source_not_imported")
        missing = _MANUAL_KINDS.copy()
        for kind, role in _TABLES.items():
            rows = database.execute(
                """
                SELECT rows.source_key, rows.record
                FROM sources JOIN source_rows AS rows ON rows.source_id = sources.id
                WHERE sources.instance = ? AND sources.role = ? AND lower(rows.table_name) = ?
                ORDER BY rows.source_key
                """,
                (instance, role, kind.value),
            )
            for source_key, encoded in rows:
                missing.discard(kind)
                records.append(_decode_record(instance, kind, source_key, encoded))
        if missing and "memory" in roles:
            records.extend(_read_snapshot_profiles(database, instance, missing, work_parent))
        if {"main", "memory"} <= roles:
            from aureus.tools.migrate.maibot_memory_rows import MemoryRows

            origin = MemoryRows(database, instance).bundle_origin()
            records = [
                record.model_copy(update={"bundle_origin": origin})
                if record.kind == HistoricalKind.PROFILE_SNAPSHOT
                else record
                for record in records
            ]
    return records


def _report(publications: list[HistoricalPublication]) -> dict[str, object]:
    tables = []
    for kind, role in _TABLES.items():
        rows = [row for row in publications if row.kind == kind]
        totals = Counter(row.status for row in rows)
        tables.append(
            {
                "role": role,
                "table": kind.value,
                "staged": len(rows),
                "written": totals["written"],
                "already_exists": totals["existing"],
                "pending": totals["pending"],
                "reasons": dict(Counter(row.reason for row in rows if row.reason is not None)),
            }
        )
    return {"operation": "publish-people", "tables": tables}


def publish_people(staging: Path, instance: str, home: Path, clock: Clock) -> dict[str, object]:
    """从显式暂存库和目标目录发布人物资料，返回脱敏的按表对账。"""
    if not instance.strip():
        raise MigrationError("invalid_source")
    if not home.is_dir():
        raise MigrationError("invalid_people_home")
    paths = AureusPaths(home.resolve())
    if paths.database_file.exists() and paths.database_file.samefile(staging):
        raise MigrationError("people_home_is_staging")
    records = _read_records(staging, instance, home)
    identity_service = IdentityService(paths)
    try:
        with ExitStack() as stack:
            stack.enter_context(identity_service.acquire_lock())
            identity = identity_service.load_identity()
            people = open_people(paths, clock, identity, stack)
            return _report(people.publish_history(records))
    except SingleInstanceLockError:
        raise MigrationError("people_home_in_use") from None
    except (IdentityError, ConfigError):
        raise MigrationError("invalid_people_identity") from None
    except PeopleError:
        raise MigrationError("people_source_conflict") from None
