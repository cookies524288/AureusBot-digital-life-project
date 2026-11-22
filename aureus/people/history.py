"""人际 people：从迁移暂存发布的人物资料与关系依据（路线图 3.3）。"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue


class PeopleError(Exception):
    """人物档案出了问题。消息是中文。"""


class HistoricalKind(StrEnum):
    """由人际子系统发布的来源表。"""

    PROFILE = "person_info"
    STATE = "person_state"
    RELATION = "relation_edge"
    EVENT = "events"
    THREAD_ITEM = "thread_item"

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


def for_person(
    conn: sqlite3.Connection, person_id: int, limit: int | None
) -> list[HistoricalEntry]:
    """查与人物相关的资料。带上限时优先选档案和最近写入的资料供回复使用。"""
    rows = conn.execute(
        """
        SELECT * FROM people_history WHERE (person_id = ? OR other_person_id = ?)
            AND (? IS NULL OR pending_reason IS NULL)
        ORDER BY kind NOT IN ('person_info', 'person_state'), id DESC LIMIT ?
        """,
        (person_id, person_id, limit, -1 if limit is None else limit),
    )
    return [_entry(row) for row in rows]


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
