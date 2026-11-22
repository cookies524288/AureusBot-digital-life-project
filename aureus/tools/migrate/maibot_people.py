"""3.3 将已暂存的 MaiBot 人际资料发布到人际子系统。"""

import json
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from pydantic import JsonValue

from aureus.common import AureusPaths, Clock, ConfigError
from aureus.core.runtime import open_people
from aureus.identity import IdentityError, IdentityService, SingleInstanceLockError
from aureus.people import HistoricalKind, HistoricalPublication, HistoricalRecord, PeopleError
from aureus.tools.migrate.maibot_catalog import MigrationError
from aureus.tools.migrate.maibot_staging import open_staging

_TABLES = {
    HistoricalKind.PROFILE: "main",
    HistoricalKind.STATE: "people",
    HistoricalKind.RELATION: "people",
    HistoricalKind.EVENT: "people",
    HistoricalKind.THREAD_ITEM: "people",
}


def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _identifier(value: JsonValue) -> str | None:
    if type(value) is int:
        return str(value)
    return _text(value)


def _record(
    instance: str, kind: HistoricalKind, source_key: str, values: dict[str, JsonValue]
) -> HistoricalRecord:
    platform = _text(values.get("platform"))
    account = _identifier(values.get("user_id"))
    if kind.is_profile:
        return HistoricalRecord(
            instance=instance,
            kind=kind,
            source_key=source_key,
            subject_key=_identifier(values.get("person_id")),
            account=(platform, account) if platform and account else None,
            name=_text(values.get("person_name")) or _text(values.get("user_nickname")),
            impression=_text(values.get("impression")),
            memory_points=values.get("memory_points"),
            known_since=values.get("first_known_time"),
            relationship_state=values.get("relationship_state"),
            original=values,
        )

    is_relation = kind is HistoricalKind.RELATION
    weight = values.get("weight")
    return HistoricalRecord(
        instance=instance,
        kind=kind,
        source_key=source_key,
        subject_key=_identifier(values.get("source_person_id" if is_relation else "person_id")),
        other_key=_identifier(values.get("target_person_id")),
        relation_type=_text(values.get("relation_type")),
        weight=(
            weight if isinstance(weight, (int, float)) and not isinstance(weight, bool) else None
        ),
        evidence=values.get("evidence"),
        occurred_at=values.get("occurred_at"),
        summary=_text(values.get("summary")),
        original=values,
    )


def _read_records(staging: Path, instance: str) -> list[HistoricalRecord]:
    records: list[HistoricalRecord] = []
    with open_staging(staging) as database:
        if (
            database.execute(
                "SELECT 1 FROM sources WHERE instance = ? AND role IN ('main', 'people')",
                (instance,),
            ).fetchone()
            is None
        ):
            raise MigrationError("source_not_imported")
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
                try:
                    data = json.loads(encoded)
                    values = data["values"]
                except (ValueError, TypeError, KeyError):
                    raise MigrationError("invalid_staged_people_record") from None
                if not isinstance(values, dict):
                    raise MigrationError("invalid_staged_people_record")
                records.append(_record(instance, kind, source_key, values))
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
    records = _read_records(staging, instance)
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
