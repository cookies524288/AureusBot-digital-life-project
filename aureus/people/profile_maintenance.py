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
    connection.execute(
        """CREATE TABLE IF NOT EXISTS local_profile_content (
        instance TEXT NOT NULL,
        person_key TEXT NOT NULL,
        summary TEXT,
        evidence TEXT,
        anchor INTEGER,
        PRIMARY KEY(instance, person_key),
        FOREIGN KEY(instance, person_key) REFERENCES historical_person_keys
    )"""
    )
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
        """SELECT summary, evidence, anchor FROM local_profile_content
           WHERE instance=? AND person_key=?""",
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


def latest_applied(connection: sqlite3.Connection, source: tuple[str, str]) -> str | None:
    """撤销只能从同一来源组最近仍生效的操作开始。"""
    row = connection.execute(
        """SELECT id FROM people_profile_plans WHERE instance=? AND person_key=?
           AND applied_at IS NOT NULL AND undone_at IS NULL
           ORDER BY applied_at DESC, rowid DESC LIMIT 1""",
        source,
    ).fetchone()
    return row["id"] if row is not None else None


def expected_version(
    plan: sqlite3.Row, now: datetime, *, undo: bool, latest: str | None
) -> str | None:
    """应用要求预览未过期且未执行；撤销要求仍是最近生效操作。"""
    if undo:
        return plan["applied_version"] if plan["id"] == latest else None
    fresh = now < datetime.fromisoformat(plan["created_at"]) + _PLAN_TTL
    return plan["expected"] if plan["applied_at"] is None and fresh else None


def invalidate_plans(
    connection: sqlite3.Connection, source: tuple[str, str], *, legacy: bool = False
) -> None:
    """作废尚未执行的预览；旧别名入口还会作废待撤销计划。"""
    connection.execute(
        """UPDATE people_profile_plans SET expected='', applied_version=NULL
           WHERE instance=? AND person_key=? AND undone_at IS NULL AND (applied_at IS NULL OR ?)""",
        (*source, legacy),
    )


def expired_before(now: datetime) -> datetime:
    """只回收未应用的短期计划。"""
    return now - _PLAN_TTL
