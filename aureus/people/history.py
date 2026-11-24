"""人际 people：从迁移暂存发布的人物资料与关系依据（路线图 3.3）。"""

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, JsonValue

from aureus.people import profile_aliases, profile_maintenance
from aureus.people.profile_aliases import ProfileAliases
from aureus.people.profile_maintenance import ProfileChangedError, ProfileEdit, ProfileSource

# A_Memorix query_person_profile 对未设 expires_at 的快照使用此默认期限。
_AUTOMATIC_PROFILE_TTL_SECONDS = 6 * 60 * 60
_EDITABLE_PROFILE = """
    history.pending_reason IS NULL AND NOT history.bundle_disabled AND EXISTS (
        SELECT 1 FROM historical_person_keys keys
        JOIN accounts ON accounts.person_id = keys.person_id
        WHERE keys.instance = history.instance AND keys.person_id = history.person_id
            AND keys.person_key = json_extract(history.record, '$.subject_key')
    )
"""


class PeopleError(Exception):
    """人物档案出了问题。消息是中文。"""


class HistoricalKind(StrEnum):
    """由人际子系统发布的来源表。"""

    PROFILE = "person_info"
    STATE = "person_state"
    RELATION = "relation_edge"
    EVENT = "events"
    THREAD_ITEM = "thread_item"
    PROFILE_SNAPSHOT = "person_profile_snapshots"
    PROFILE_OVERRIDE = "person_profile_overrides"
    ALIAS_OVERRIDE = "person_profile_alias_overrides"
    LOCAL_ALIAS_OVERRIDE = "local_profile_aliases"
    LOCAL_PROFILE_OVERRIDE = "local_profile_content"

    @property
    def is_profile(self) -> bool:
        """来源表是否提供人物资料。"""
        return self in (HistoricalKind.PROFILE, HistoricalKind.STATE)


class HistoricalRecord(BaseModel):
    """发布接口的人际记录。来源键为调用方传入的稳定标识。"""

    model_config = ConfigDict(frozen=True)

    instance: str
    kind: HistoricalKind
    source_key: str
    # 人物键由发布方明确对应到同一实例的 person_info 人物键。
    subject_key: str | None = None
    other_key: str | None = None
    account: tuple[str, str] | None = None
    name: str | None = None
    nickname: str | None = None
    group_cardname: JsonValue = None
    name_reason: str | None = None
    impression: str | None = None
    memory_points: JsonValue = None
    known_since: JsonValue = None
    relationship_state: JsonValue = None
    relation_type: str | None = None
    event_type: str | None = None
    weight: int | float | None = None
    evidence: JsonValue = None
    occurred_at: JsonValue = None
    summary: str | None = None
    due_at: JsonValue = None
    hardness: JsonValue = None
    profile_version: int | None = None
    aliases: list[str] | None = None
    expires_at: JsonValue = None
    original: dict[str, JsonValue] | None = None
    bundle_origin: str | None = None


@dataclass(frozen=True)
class HistoricalEntry:
    """保留方向、原值和解析状态的人际资料。"""

    id: int
    record: HistoricalRecord
    person_id: int | None
    other_person_id: int | None
    pending_reason: str | None


@dataclass(frozen=True)
class HistoricalPublication:
    """一条发布结果，只提供来源表、状态和原因供对账。"""

    kind: HistoricalKind
    status: str
    reason: str | None


def init_schema(conn: sqlite3.Connection) -> None:
    """建立人际子系统拥有的历史资料和人物来源键表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_person_keys (
            instance TEXT NOT NULL,
            person_key TEXT NOT NULL,
            person_id INTEGER NOT NULL REFERENCES people(id),
            PRIMARY KEY (instance, person_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS people_history (
            id INTEGER PRIMARY KEY,
            instance TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_key TEXT NOT NULL,
            record TEXT NOT NULL,
            person_id INTEGER REFERENCES people(id),
            other_person_id INTEGER REFERENCES people(id),
            pending_reason TEXT,
            UNIQUE (instance, kind, source_key)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS people_history_subject ON people_history(person_id)")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(people_history)")}
    if "bundle_disabled" not in columns:
        conn.execute(
            "ALTER TABLE people_history ADD COLUMN bundle_disabled INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS people_bundle_profiles (
            installation_id TEXT NOT NULL, profile_id INTEGER NOT NULL, installed INTEGER NOT NULL,
            PRIMARY KEY (installation_id, profile_id)
        ) WITHOUT ROWID
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS people_bundles_profile ON people_bundle_profiles(profile_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS people_history_other ON people_history(other_person_id)"
    )
    profile_maintenance.init_schema(conn)


def open_flow(conn: sqlite3.Connection) -> None:
    """公开历史画像与本地资料合并事件的只读投影。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS people_account_merges (
            id INTEGER PRIMARY KEY,
            happened REAL NOT NULL,
            summary TEXT NOT NULL
        )
        """)
    conn.execute("""
        CREATE VIEW IF NOT EXISTS people_memory_events AS
        SELECT json_array(history.instance, 'memory', history.kind, json(source_key)) AS source,
               CASE WHEN history.kind != 'person_profile_snapshots' THEN 'profile_override_set'
                    WHEN json_extract(record, '$.profile_version') = 1 THEN 'profile_created'
                    WHEN json_extract(record, '$.profile_version') > 1 THEN 'profile_updated'
                    ELSE 'profile_snapshot' END AS event_type,
               CASE WHEN json_type(record, '$.occurred_at') IN ('integer', 'real')
                    THEN json_extract(record, '$.occurred_at') END AS happened,
               coalesce(nullif(person.name, ''), json_extract(record, '$.subject_key'), '人物')
                 || CASE WHEN history.kind != 'person_profile_snapshots' THEN ' · 手工维护画像'
                         ELSE ' · 画像版本 '
                              || coalesce(json_extract(record, '$.profile_version'), '未记录')
                    END AS summary,
               '[]' AS conversations
        FROM people_history history LEFT JOIN people person ON person.id = history.person_id
        WHERE history.kind IN ('person_profile_snapshots', 'person_profile_overrides',
                       'person_profile_alias_overrides') AND json_valid(source_key)
        UNION ALL
        SELECT NULL, 'person_merged', happened, summary, '[]' FROM people_account_merges
        """)


def _entry(row: sqlite3.Row) -> HistoricalEntry:
    return HistoricalEntry(
        row["id"],
        HistoricalRecord.model_validate_json(row["record"]),
        row["person_id"],
        row["other_person_id"],
        row["pending_reason"],
    )


def get_record(
    conn: sqlite3.Connection, instance: str, kind: HistoricalKind, source_key: str
) -> HistoricalEntry | None:
    """按完整来源定位已发布或待解析的原始资料。"""
    row = conn.execute(
        "SELECT * FROM people_history WHERE instance = ? AND kind = ? AND source_key = ?",
        (instance, kind, source_key),
    ).fetchone()
    return _entry(row) if row is not None else None


def for_person(conn: sqlite3.Connection, person_id: int) -> list[HistoricalEntry]:
    """查与人物相关的完整资料，包括所有可关联的画像版本。"""
    rows = conn.execute(
        """
        SELECT * FROM people_history WHERE (person_id = ? OR other_person_id = ?)
        ORDER BY kind NOT IN ('person_info', 'person_state'), id DESC
        """,
        (person_id, person_id),
    )
    return [_entry(row) for row in rows]


def browse_profiles(
    conn: sqlite3.Connection, query: str, offset: int, limit: int
) -> tuple[list[HistoricalEntry], int]:
    """分页查找人物画像和维护覆盖及其筛选总数，包含待解析的来源。"""
    conn.execute("BEGIN")
    filtered = """FROM people_history history
           LEFT JOIN people person ON person.id = history.person_id
           WHERE history.kind IN ('person_profile_snapshots', 'person_profile_overrides',
                                  'person_profile_alias_overrides')
             AND NOT history.bundle_disabled
             AND (? = '' OR instr(lower(history.record), lower(?)) > 0
                         OR instr(lower(person.name), lower(?)) > 0)"""
    params = (query, query, query)
    total = conn.execute(f"SELECT count(*) {filtered}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT history.* {filtered} ORDER BY history.id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return [_entry(row) for row in rows], total


def browse_profile_resources(
    conn: sqlite3.Connection, query: str, offset: int, limit: int
) -> tuple[list[dict[str, object]], int]:
    """浏览人物档案索引及画像原件，区分未关联画像和没有画像正文的人物。"""
    conn.execute("BEGIN")
    catalog = f"""
        WITH entries AS (
            SELECT 'person' AS entry_type, person.id, person.name,
                   person.id AS person_id, NULL AS other_person_id, NULL AS pending_reason,
                   person.first_seen, NULL AS record,
                   unixepoch(person.first_seen, 'subsec') AS happened, 0 AS can_edit_aliases
            FROM people person
            UNION ALL
            SELECT 'profile', history.id, person.name, history.person_id, history.other_person_id,
                   history.pending_reason, NULL, history.record,
                   CASE WHEN json_type(history.record, '$.occurred_at') IN ('real', 'integer')
                        THEN json_extract(history.record, '$.occurred_at') END,
                   ({_EDITABLE_PROFILE})
            FROM people_history history LEFT JOIN people person ON person.id = history.person_id
            WHERE history.kind IN ('person_profile_snapshots', 'person_profile_overrides',
                                   'person_profile_alias_overrides')
                AND NOT history.bundle_disabled
        ), matching AS (
            SELECT * FROM entries
            WHERE :query = '' OR instr(lower(name), lower(:query)) > 0
                OR instr(lower(record), lower(:query)) > 0 OR CAST(person_id AS TEXT) = :query
        )
    """
    params = {"query": query, "limit": limit, "offset": offset}
    total = conn.execute(catalog + "SELECT count(*) FROM matching", params).fetchone()[0]
    rows = conn.execute(
        catalog + "SELECT * FROM matching ORDER BY happened DESC NULLS LAST, entry_type, id DESC"
        " LIMIT :limit OFFSET :offset",
        params,
    )
    items = [
        {
            "entry_type": "person",
            "person_id": row["person_id"],
            "name": row["name"],
            "first_seen": row["first_seen"],
        }
        if row["entry_type"] == "person"
        else {
            "entry_type": "profile",
            "name": row["name"],
            "can_edit_aliases": bool(row["can_edit_aliases"]),
            **asdict(_entry(row)),
        }
        for row in rows
    ]
    return items, total


def _snapshot_is_current(record: HistoricalRecord, now: float) -> bool:
    expires = record.expires_at
    if expires is not None:
        return isinstance(expires, (int, float)) and not isinstance(expires, bool) and now < expires
    updated = record.occurred_at
    return (
        isinstance(updated, (int, float))
        and not isinstance(updated, bool)
        and now < updated + _AUTOMATIC_PROFILE_TTL_SECONDS
    )


def _latest_profiles(
    conn: sqlite3.Connection, person_id: int
) -> dict[tuple[str, str | None], dict[HistoricalKind, HistoricalEntry]]:
    rows = conn.execute(
        """
        WITH revisions AS (
            SELECT id, row_number() OVER (
                PARTITION BY instance, json_extract(record, '$.subject_key'), kind
                ORDER BY json_extract(record, '$.profile_version') DESC, id DESC
            ) AS revision
            FROM people_history
            WHERE person_id = ? AND pending_reason IS NULL AND NOT bundle_disabled
                AND kind IN ('person_profile_snapshots', 'person_profile_overrides',
                             'person_profile_alias_overrides')
        )
        SELECT people_history.* FROM revisions JOIN people_history USING (id)
        WHERE revision = 1 ORDER BY id DESC
        """,
        (person_id,),
    )
    profiles: dict[tuple[str, str | None], dict[HistoricalKind, HistoricalEntry]] = {}
    for row in rows:
        entry = _entry(row)
        record = entry.record
        profiles.setdefault((record.instance, record.subject_key), {})[record.kind] = entry
    return profiles


@dataclass(frozen=True)
class ProfileTarget:
    """当前可信来源组及其生效值。"""

    key: tuple[str, str]
    version: str
    edit: ProfileEdit
    summary: str | None
    aliases: list[str]
    evidence: list[JsonValue]
    anchor: int | None


def _base_profile(
    group: dict[HistoricalKind, HistoricalEntry],
    now: float,
) -> tuple[HistoricalEntry | None, HistoricalEntry | None, list[str]]:
    snapshot = group.get(HistoricalKind.PROFILE_SNAPSHOT)
    if snapshot is not None and not _snapshot_is_current(snapshot.record, now):
        snapshot = None
    manual = group.get(HistoricalKind.PROFILE_OVERRIDE)
    primary = manual if manual is not None and manual.record.summary else snapshot
    explicit_alias = group.get(HistoricalKind.ALIAS_OVERRIDE)
    aliases = explicit_alias.record.aliases if explicit_alias is not None else None
    original_aliases = snapshot.record.aliases or [] if snapshot is not None else []
    return primary, snapshot, aliases if aliases is not None else original_aliases


def _evidence_choices(evidence: JsonValue) -> list[JsonValue]:
    return evidence if isinstance(evidence, list) else ([] if evidence is None else [evidence])


def _profile_target(
    conn: sqlite3.Connection,
    source: ProfileSource,
    now: datetime,
) -> ProfileTarget | None:
    row = conn.execute(
        f"""SELECT history.person_id, json_extract(record, '$.subject_key') AS person_key
            FROM people_history history WHERE instance=? AND kind=? AND source_key=?
            AND {_EDITABLE_PROFILE} AND kind IN ('person_profile_snapshots',
                'person_profile_overrides', 'person_profile_alias_overrides')""",
        (source.instance, source.kind, source.source_key),
    ).fetchone()
    if row is None:
        return None
    person_id = row["person_id"]
    key = source.instance, row["person_key"]
    group = _latest_profiles(conn, person_id)[key]
    primary, _, original_aliases = _base_profile(group, now.timestamp())
    original = primary.record if primary is not None else None
    evidence = original.evidence if original is not None else None
    choices = _evidence_choices(evidence)
    summary, positions, anchor = profile_maintenance.content(conn, key)
    current_anchor = primary.id if primary is not None else None
    aliases = profile_aliases.for_person(conn, person_id).get(key)
    edit = ProfileEdit(summary=summary, aliases=aliases, evidence=positions, anchor=anchor)
    # 版本只取来源组与生效覆盖，不含计划行；撤销较新的计划后能还原较早计划的版本（3.15 自检）。
    version = sha256(
        json.dumps(
            [person_id, [item.id for item in group.values()], current_anchor, edit.model_dump()],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return ProfileTarget(
        key,
        version,
        edit,
        original.summary if original else None,
        original_aliases,
        choices,
        current_anchor,
    )


def _profile_view(target: ProfileTarget, edit: ProfileEdit) -> dict[str, object]:
    anchored = edit.anchor == target.anchor
    return {
        "summary": edit.summary if anchored and edit.summary is not None else target.summary,
        "aliases": edit.aliases if edit.aliases is not None else target.aliases,
        "evidence": (
            list(range(len(target.evidence)))
            if edit.evidence is None
            else edit.evidence
            if anchored
            else []
        ),
    }


def profile_details(
    conn: sqlite3.Connection,
    source: ProfileSource,
    now: datetime,
    active: Callable[[], bool],
) -> dict[str, object] | None:
    """回读实际生效画像与仍可撤销的已执行操作。"""
    with conn:
        conn.execute("BEGIN")
        target = _profile_target(conn, source, now)
        if target is None or not active():
            return None
        latest = conn.execute(
            """SELECT id, applied_at, undone_at, applied_version FROM people_profile_plans
               WHERE instance=? AND person_key=? AND applied_at IS NOT NULL
               ORDER BY rowid DESC LIMIT 20""",
            target.key,
        )
        return {
            "choices": target.evidence,
            "effective": _profile_view(target, target.edit),
            "edit": target.edit.model_dump(exclude={"anchor"}),
            "history": [
                dict(
                    id=row["id"],
                    applied_at=row["applied_at"],
                    undone_at=row["undone_at"],
                    can_undo=row["applied_version"] == target.version and not row["undone_at"],
                )
                for row in latest
            ],
        }


def preview_profile(
    conn: sqlite3.Connection,
    source: ProfileSource,
    edit: ProfileEdit,
    now: datetime,
    active: Callable[[], bool],
) -> dict[str, object] | None:
    """只为当前有效来源生成计划，位置不得跨同名人物或版本。"""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        target = _profile_target(conn, source, now)
        if target is None or not active():
            return None
        if edit.evidence is not None and (
            len(set(edit.evidence)) != len(edit.evidence)
            or any(not 0 <= point < len(target.evidence) for point in edit.evidence)
        ):
            raise ValueError("只能选择本组当前已有的证据位置。")
        summary = edit.summary.strip() if edit.summary is not None else None
        if summary == "":
            raise ValueError("画像正文不能只包含空白。")
        if (summary is not None or edit.evidence is not None) and target.anchor is None:
            raise ProfileChangedError("原画像已失效，不能检修正文或证据。")
        aliases = (
            profile_aliases.normalize_aliases(edit.aliases) if edit.aliases is not None else None
        )
        proposed = ProfileEdit(
            summary=summary,
            aliases=aliases,
            evidence=edit.evidence,
            anchor=target.anchor if summary is not None or edit.evidence is not None else None,
        )
        if proposed == target.edit:
            raise ProfileChangedError("没有需要保存的画像变化。")
        plan_id = uuid4().hex
        conn.execute(
            "DELETE FROM people_profile_plans WHERE applied_at IS NULL AND created_at < ?",
            (profile_maintenance.expired_before(now).isoformat(),),
        )
        conn.execute(
            """INSERT INTO people_profile_plans
               (id, instance, kind, source_key, person_key, created_at, expected, previous,
                proposed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan_id,
                source.instance,
                source.kind,
                source.source_key,
                target.key[1],
                now.isoformat(),
                target.version,
                target.edit.model_dump_json(),
                proposed.model_dump_json(),
            ),
        )
        return {
            "plan_id": plan_id,
            "version": target.version,
            "choices": target.evidence,
            "before": _profile_view(target, target.edit),
            "after": _profile_view(target, proposed),
        }


def change_profile(
    conn: sqlite3.Connection,
    identifier: str,
    now: datetime,
    active: Callable[[], bool],
    *,
    undo: bool,
) -> bool:
    """应用或撤销在同一写事务中复核账号关联、来源和版本。"""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM people_profile_plans WHERE id=?", (identifier,)
        ).fetchone()
        if row is None or not active():
            return False
        source = ProfileSource(row["instance"], row["kind"], row["source_key"])
        target = _profile_target(conn, source, now)
        if target is None:
            return False
        if profile_maintenance.expected_version(row, now, undo=undo) != target.version:
            raise ProfileChangedError("原件或版本已变化，请重新预览。")
        edit = ProfileEdit.model_validate_json(row["previous" if undo else "proposed"])
        profile_maintenance.write_content(conn, target.key, edit)
        profile_aliases.write(conn, target.key, edit.aliases)
        stamp = "undone_at" if undo else "applied_at"
        conn.execute(
            f"UPDATE people_profile_plans SET {stamp}=? WHERE id=?", (now.isoformat(), identifier)
        )
        current = _profile_target(conn, source, now)
        if current is None or not active():
            raise ProfileChangedError("当前权限已变化，未保存更改。")
        if not undo:
            conn.execute(
                "UPDATE people_profile_plans SET applied_version=? WHERE id=?",
                (current.version, identifier),
            )
        return True


def bundle_profile_candidates(
    connection: sqlite3.Connection, origins: dict[str, str], now: datetime
) -> list[tuple[str, dict[str, object]]]:
    """提供明确绑定人物的当前迁入快照，保留闭包校验所需的引用。"""
    rows = connection.execute(
        f"""SELECT DISTINCT history.person_id FROM people_history history
            WHERE history.instance IN (SELECT value FROM json_each(?))
                AND history.kind = 'person_profile_snapshots' AND {_EDITABLE_PROFILE}
            ORDER BY history.person_id""",
        (json.dumps(list(origins)),),
    ).fetchall()
    candidates: list[tuple[str, dict[str, object]]] = []
    for (person_id,) in rows:
        for (instance, subject), group in _latest_profiles(connection, person_id).items():
            snapshot = group.get(HistoricalKind.PROFILE_SNAPSHOT)
            if (
                instance not in origins
                or not subject
                or snapshot is None
                or HistoricalKind.PROFILE_OVERRIDE in group
                or not _snapshot_is_current(snapshot.record, now.timestamp())
            ):
                continue
            record = snapshot.record
            if not origins[instance] or record.bundle_origin != origins[instance]:
                continue
            summary, positions, anchor = profile_maintenance.content(
                connection, (instance, subject)
            )
            if anchor == snapshot.id and (summary is not None or positions is not None):
                continue
            original = record.original or {}
            if not record.summary or type(record.profile_version) is not int:
                continue
            candidates.append(
                (
                    instance,
                    {
                        "snapshot_id": record.source_key,
                        "person_id": subject,
                        "profile_text": record.summary,
                        "profile_version": record.profile_version,
                        "updated_at": record.occurred_at,
                        "expires_at": record.expires_at,
                        "evidence_ids_json": original.get("evidence_ids_json"),
                        "fact_claim_ids_json": original.get("fact_claim_ids_json"),
                    },
                )
            )
    return candidates


def profile_alias_details(
    conn: sqlite3.Connection,
    profile_id: int,
    now: float,
    *,
    write: bool = False,
    aliases: list[str] | None = None,
) -> ProfileAliases | None:
    """定位已有画像来源，按需写入本机别名，并返回当前生效集合。"""
    target = conn.execute(
        f"""SELECT instance, person_id, json_extract(record, '$.subject_key') AS person_key
            FROM people_history history WHERE id = ? AND {_EDITABLE_PROFILE}
            AND kind IN ('person_profile_snapshots', 'person_profile_overrides',
                         'person_profile_alias_overrides')""",
        (profile_id,),
    ).fetchone()
    if target is None:
        return None
    person_id = int(target["person_id"])
    source = (str(target["instance"]), str(target["person_key"]))
    if write:
        profile_aliases.write(conn, source, aliases)
    group = _latest_profiles(conn, person_id)[source]
    manual = group.get(HistoricalKind.ALIAS_OVERRIDE)
    snapshot = group.get(HistoricalKind.PROFILE_SNAPSHOT)
    if manual is not None and manual.record.aliases is not None:
        original = manual.record.aliases
    elif snapshot is not None and _snapshot_is_current(snapshot.record, now):
        original = snapshot.record.aliases or []
    else:
        original = []
    return ProfileAliases(original, profile_aliases.for_person(conn, person_id).get(source))


def _profiles_for_recall(
    conn: sqlite3.Connection,
    person_id: int,
    now: float,
    *,
    use_local_aliases: bool,
    use_local_content: bool,
) -> list[HistoricalEntry]:
    local = profile_aliases.for_person(conn, person_id) if use_local_aliases else {}

    selected: list[HistoricalEntry] = []
    for source, group in _latest_profiles(conn, person_id).items():
        manual = group.get(HistoricalKind.PROFILE_OVERRIDE)
        manual_alias = group.get(HistoricalKind.ALIAS_OVERRIDE)
        has_manual_text = manual is not None and bool(manual.record.summary)
        has_manual_alias = manual_alias is not None and manual_alias.record.aliases is not None
        primary, snapshot, _ = _base_profile(group, now)
        instance, subject_key = source
        summary, positions, anchor_id = (
            profile_maintenance.content(conn, (instance, subject_key))
            if use_local_content and subject_key is not None
            else (None, None, None)
        )
        anchor = primary or next(iter(group.values()))
        if primary is None or primary.id != anchor_id:
            summary = None
        if summary is not None or positions is not None:
            choices = _evidence_choices(primary.record.evidence) if primary else []
            evidence = None
            if positions is not None:
                evidence = (
                    [choices[point] for point in positions]
                    if primary and primary.id == anchor_id
                    else []
                )
            selected.append(
                replace(
                    anchor,
                    record=HistoricalRecord(
                        instance=instance,
                        kind=HistoricalKind.LOCAL_PROFILE_OVERRIDE,
                        source_key=subject_key or "",
                        subject_key=subject_key,
                        summary=summary
                        if summary is not None
                        else (primary.record.summary if primary else None),
                        evidence=evidence,
                    ),
                )
            )
        elif manual is not None and has_manual_text:
            selected.append(manual)
        aliases = local.get((instance, subject_key)) if subject_key is not None else None
        if aliases is not None:
            selected.append(
                replace(
                    anchor,
                    record=HistoricalRecord(
                        instance=instance,
                        kind=HistoricalKind.LOCAL_ALIAS_OVERRIDE,
                        source_key=subject_key or "",
                        subject_key=subject_key,
                        aliases=aliases,
                    ),
                )
            )
        elif manual_alias is not None and manual_alias.record.aliases:
            selected.append(manual_alias)
        if snapshot is None:
            continue
        record = snapshot.record
        summary = (
            None
            if has_manual_text or summary is not None or positions is not None
            else record.summary
        )
        aliases = None if has_manual_alias or aliases is not None else record.aliases
        if summary or aliases:
            selected.append(
                replace(
                    snapshot,
                    record=record.model_copy(
                        update={
                            "summary": summary,
                            "aliases": aliases,
                        }
                    ),
                )
            )
    return selected


def for_recall(
    conn: sqlite3.Connection,
    person_id: int,
    limit: int,
    now: float,
    *,
    use_local_aliases: bool,
    use_local_content: bool,
) -> list[HistoricalEntry]:
    """选有效画像和普通人际资料，避免旧画像占用召回预算。"""
    profiles = _profiles_for_recall(
        conn,
        person_id,
        now,
        use_local_aliases=use_local_aliases,
        use_local_content=use_local_content,
    )
    if 0 <= limit <= len(profiles):
        return profiles[:limit]
    rows = conn.execute(
        """
        SELECT * FROM people_history WHERE (person_id = ? OR other_person_id = ?)
            AND pending_reason IS NULL
            AND kind NOT IN ('person_profile_snapshots', 'person_profile_overrides',
                             'person_profile_alias_overrides')
        ORDER BY kind NOT IN ('person_info', 'person_state'), id DESC LIMIT ?
        """,
        (person_id, person_id, limit - len(profiles)),
    )
    return profiles + [_entry(row) for row in rows]


def _person_id(conn: sqlite3.Connection, instance: str, key: str | None) -> int | None:
    row = conn.execute(
        "SELECT person_id FROM historical_person_keys WHERE instance = ? AND person_key = ?",
        (instance, key),
    ).fetchone()
    return row["person_id"] if row is not None else None


def _bind_account(
    conn: sqlite3.Connection,
    record: HistoricalRecord,
    meet: Callable[[str, str], int],
) -> int | None:
    if not record.kind.is_profile or record.account is None or not all(record.account):
        return None
    person_id = meet(*record.account)
    if record.subject_key:
        existing = _person_id(conn, record.instance, record.subject_key)
        if existing is not None and existing != person_id:
            raise PeopleError("迁移人物键对应多个不同的人物档案")
        conn.execute(
            "INSERT INTO historical_person_keys VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (record.instance, record.subject_key, person_id),
        )
    return person_id


def _resolve(
    conn: sqlite3.Connection, record: HistoricalRecord, account_owner: int | None
) -> tuple[int | None, int | None, str | None]:
    subject = account_owner or _person_id(conn, record.instance, record.subject_key)
    if record.kind.is_profile:
        return subject, None, "missing_account" if subject is None else None
    other = _person_id(conn, record.instance, record.other_key)
    if record.kind is HistoricalKind.RELATION and (subject is None or other is None):
        return None, None, "unresolved_relation_endpoint"
    if (record.subject_key and subject is None) or (record.other_key and other is None):
        return subject, other, "unresolved_person_reference"
    if subject is None and other is None:
        return None, None, "missing_person_reference"
    return subject, other, None


def _publish_record(
    conn: sqlite3.Connection, record: HistoricalRecord, account_owner: int | None
) -> HistoricalPublication:
    existing = get_record(conn, record.instance, record.kind, record.source_key)
    if existing is not None and existing.record != record:
        raise PeopleError("同一人物资料来源键的内容已变更")
    if existing is not None and existing.pending_reason is None:
        return HistoricalPublication(record.kind, "existing", None)

    subject, other, reason = _resolve(conn, record, account_owner)
    if existing is None:
        conn.execute(
            """
            INSERT INTO people_history
                (instance, kind, source_key, record, person_id, other_person_id, pending_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.instance,
                record.kind,
                record.source_key,
                record.model_dump_json(),
                subject,
                other,
                reason,
            ),
        )
    elif (subject, other, reason) != (
        existing.person_id,
        existing.other_person_id,
        existing.pending_reason,
    ):
        conn.execute(
            "UPDATE people_history SET person_id = ?, other_person_id = ?, pending_reason = ? "
            "WHERE id = ?",
            (subject, other, reason, existing.id),
        )
    return HistoricalPublication(record.kind, "pending" if reason else "written", reason)


def publish(
    conn: sqlite3.Connection, records: Sequence[HistoricalRecord], meet: Callable[[str, str], int]
) -> list[HistoricalPublication]:
    """原子发布一个批次。先绑定明确账号，再解析引用，结果顺序与输入一致。"""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        owners = [_bind_account(conn, record, meet) for record in records]
        return [
            _publish_record(conn, record, owner)
            for record, owner in zip(records, owners, strict=True)
        ]
