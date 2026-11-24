"""人际 people：从迁移暂存发布的人物资料与关系依据（路线图 3.3）。"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue

from aureus.people import profile_aliases
from aureus.people.profile_aliases import ProfileAliases

# A_Memorix query_person_profile 对未设 expires_at 的快照使用此默认期限。
_AUTOMATIC_PROFILE_TTL_SECONDS = 6 * 60 * 60
_EDITABLE_PROFILE = """
    history.pending_reason IS NULL AND EXISTS (
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS people_history_other ON people_history(other_person_id)"
    )


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
            WHERE person_id = ? AND pending_reason IS NULL
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
) -> list[HistoricalEntry]:
    local = profile_aliases.for_person(conn, person_id) if use_local_aliases else {}

    selected: list[HistoricalEntry] = []
    for source, group in _latest_profiles(conn, person_id).items():
        manual = group.get(HistoricalKind.PROFILE_OVERRIDE)
        manual_alias = group.get(HistoricalKind.ALIAS_OVERRIDE)
        has_manual_text = manual is not None and bool(manual.record.summary)
        has_manual_alias = manual_alias is not None and manual_alias.record.aliases is not None
        if manual is not None and has_manual_text:
            selected.append(manual)
        snapshot = group.get(HistoricalKind.PROFILE_SNAPSHOT)
        instance, subject_key = source
        aliases = local.get((instance, subject_key)) if subject_key is not None else None
        if aliases is not None:
            anchor = next(iter(group.values()))
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
        if snapshot is None or not _snapshot_is_current(snapshot.record, now):
            continue
        record = snapshot.record
        summary = None if has_manual_text else record.summary
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
    conn: sqlite3.Connection, person_id: int, limit: int, now: float, *, use_local_aliases: bool
) -> list[HistoricalEntry]:
    """选有效画像和普通人际资料，避免旧画像占用召回预算。"""
    profiles = _profiles_for_recall(conn, person_id, now, use_local_aliases=use_local_aliases)
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
