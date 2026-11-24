"""按画像来源组保存本机正文、证据覆盖与可撤销计划。"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

_PLAN_TTL = timedelta(minutes=15)


class ProfileChangedError(ValueError):
    """画像来源或当前版本不再满足计划。"""


class ProfileEdit(BaseModel):
    """None 保留来源值，空列表可明确不使用已有证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str | None = Field(default=None, min_length=1, max_length=20000)
    aliases: list[str] | None = Field(default=None, max_length=100)
    evidence: list[int] | None = Field(default=None, max_length=100)
    anchor: int | None = None


@dataclass(frozen=True)
class ProfileSource:
    """组键用于写覆盖；原件仍须通过实例、表、来源键定位。"""

    instance: str
    kind: str
    source_key: str


def init_schema(connection: sqlite3.Connection) -> None:
    """只在可信的人物来源键上建立一层本机覆盖。"""
    connection.execute("""CREATE TABLE IF NOT EXISTS local_profile_content (
        instance TEXT NOT NULL, person_key TEXT NOT NULL, summary TEXT, evidence TEXT, anchor INTEGER,
        PRIMARY KEY(instance, person_key),
        FOREIGN KEY(instance, person_key) REFERENCES historical_person_keys
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS people_profile_plans (
        id TEXT PRIMARY KEY, instance TEXT NOT NULL, kind TEXT NOT NULL, source_key TEXT NOT NULL,
        person_key TEXT NOT NULL, created_at TEXT NOT NULL, expected TEXT NOT NULL,
        previous TEXT NOT NULL, proposed TEXT NOT NULL, applied_version TEXT,
        applied_at TEXT, undone_at TEXT
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS people_profile_plan_source
        ON people_profile_plans(instance, person_key)""")


def content(
    connection: sqlite3.Connection,
    source: tuple[str, str],
) -> tuple[str | None, list[int] | None, int | None]:
    """取本机挂接的正文与原证据位置，不复制证据内容。"""
    row = connection.execute(
        "SELECT summary, evidence, anchor FROM local_profile_content WHERE instance=? AND person_key=?",
        source,
    ).fetchone()
    return (
        (row[0], json.loads(row[1]) if row[1] is not None else None, row[2])
        if row
        else (None, None, None)
    )


def write_content(
    connection: sqlite3.Connection, source: tuple[str, str], edit: ProfileEdit
) -> None:
    """持久化覆盖；原件与计划历史不受修改。"""
    connection.execute(
        """INSERT INTO local_profile_content VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(instance, person_key) DO UPDATE SET
               summary=excluded.summary, evidence=excluded.evidence, anchor=excluded.anchor""",
        (
            *source,
            edit.summary,
            json.dumps(edit.evidence) if edit.evidence is not None else None,
            edit.anchor,
        ),
    )


def expected_version(plan: sqlite3.Row, now: datetime, *, undo: bool) -> str | None:
    """只允许未过期的计划执行一次；撤销使用应用后的版本。"""
    if undo:
        return plan["applied_version"] if plan["undone_at"] is None else None
    fresh = now < datetime.fromisoformat(plan["created_at"]) + _PLAN_TTL
    return plan["expected"] if plan["applied_at"] is None and fresh else None


def expired_before(now: datetime) -> datetime:
    """只回收未应用的短期计划。"""
    return now - _PLAN_TTL
