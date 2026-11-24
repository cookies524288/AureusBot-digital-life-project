"""人物画像包原件及可逆安装归属。"""

import json
import math
import sqlite3
from typing import Any, cast

from pydantic import JsonValue

from aureus.people.history import HistoricalKind, HistoricalRecord

_KINDS = (
    HistoricalKind.PROFILE_SNAPSHOT,
    HistoricalKind.PROFILE_OVERRIDE,
    HistoricalKind.ALIAS_OVERRIDE,
)


def _items(row: dict[str, Any], field: str) -> list[str] | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ValueError(f"人物画像的 {field} 无法解析。") from error
    if not isinstance(value, list) or not all(isinstance(item, (str, int)) for item in value):
        raise ValueError(f"人物画像的 {field} 不是有效数组。")
    return [str(item) for item in value]


def _stamp(row: dict[str, Any], field: str) -> float | None:
    value = row.get(field)
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"人物画像的 {field} 不是有效时间。")
    return value


def prepare(rows: dict[str, list[dict[str, Any]]], digest: str) -> list[HistoricalRecord]:
    """校验快照版本及时间，按上游顺序保留证据原始键。"""
    result = []
    for kind in _KINDS:
        for row in rows.get(kind, []):
            subject = str(row.get("person_id") or "").strip()
            if not subject:
                raise ValueError("人物画像缺少 person_id。")
            snapshot = kind == HistoricalKind.PROFILE_SNAPSHOT
            version = row.get("profile_version") if snapshot else None
            if snapshot and (type(version) is not int or version < 0):
                raise ValueError("人物画像快照缺少有效版本。")
            summary = row.get("profile_text" if snapshot else "override_text")
            if kind != HistoricalKind.ALIAS_OVERRIDE and not isinstance(summary, str):
                raise ValueError("人物画像缺少有效正文。")
            aliases = _items(row, "aliases_json")
            if kind == HistoricalKind.ALIAS_OVERRIDE and aliases is None:
                raise ValueError("人物画像别名覆盖缺少别名数组。")
            links = {
                label: items
                for label, field in (
                    ("memory_ids", "evidence_ids_json"),
                    ("fact_claim_ids", "fact_claim_ids_json"),
                )
                if (items := _items(row, field))
            }
            result.append(
                HistoricalRecord(
                    instance=f"amembundle:{digest}",
                    kind=kind,
                    subject_key=subject,
                    source_key=str(row["snapshot_id"]) if snapshot else subject,
                    profile_version=version,
                    summary=summary or None,
                    aliases=aliases,
                    occurred_at=_stamp(row, "updated_at"),
                    expires_at=_stamp(row, "expires_at"),
                    original=row,
                    evidence=cast(JsonValue, links or None),
                )
            )
    return result


def _binding(connection: sqlite3.Connection, key: str) -> tuple[str, int] | None:
    rows = connection.execute(
        """
        SELECT historic.instance, historic.person_id FROM historical_person_keys historic
        WHERE historic.person_key = ? AND EXISTS (
            SELECT 1 FROM accounts owner WHERE owner.person_id = historic.person_id
        ) ORDER BY historic.instance
    """,
        (key,),
    ).fetchall()
    if not rows or len({row["person_id"] for row in rows}) != 1:
        return None
    return (rows[0]["instance"], rows[0]["person_id"])


def person_key(connection: sqlite3.Connection, key: str) -> str | None:
    """只映射已明确绑定账号的唯一人物键。"""
    mapping = _binding(connection, key)
    return f"maibot:{mapping[0]}:{key}" if mapping else None


def occupied(connection: sqlite3.Connection) -> bool:
    """卸载或待映射的人物画像原件也属于目标库。"""
    return bool(
        connection.execute(
            """
        SELECT 1 FROM people_history WHERE kind IN (?, ?, ?) LIMIT 1
    """,
            _KINDS,
        ).fetchone()
    )


def publish(
    connection: sqlite3.Connection, installation_id: str, records: list[HistoricalRecord]
) -> None:
    """在调用方事务中发布历史画像，不以包内姓名或账号建档。"""
    for record in records:
        mapping = _binding(connection, record.subject_key or "")
        connection.execute(
            """
            INSERT INTO people_history (
                instance, kind, source_key, record, person_id, pending_reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance, kind, source_key) DO NOTHING
        """,
            (
                record.instance,
                record.kind,
                record.source_key,
                record.model_dump_json(),
                mapping[1] if mapping else None,
                None if mapping else "unresolved_person_reference",
            ),
        )
        connection.execute(
            """
            INSERT INTO people_bundle_profiles (installation_id, profile_id, installed)
            SELECT ?, id, 1 FROM people_history WHERE instance = ? AND kind = ? AND source_key = ?
        """,
            (installation_id, record.instance, record.kind, record.source_key),
        )
    set_installed(connection, installation_id, True)


def set_installed(connection: sqlite3.Connection, installation_id: str, installed: bool) -> None:
    """只更改包归属；若其他包仍有引用则保留画像。"""
    connection.execute(
        """
        UPDATE people_bundle_profiles SET installed = ? WHERE installation_id = ?
    """,
        (installed, installation_id),
    )
    connection.execute(
        """
        UPDATE people_history AS history SET bundle_disabled = NOT EXISTS (
            SELECT 1 FROM people_bundle_profiles other
            WHERE other.profile_id = history.id AND other.installed
        ) WHERE id IN (SELECT profile_id FROM people_bundle_profiles WHERE installation_id = ?)
    """,
        (installation_id,),
    )
