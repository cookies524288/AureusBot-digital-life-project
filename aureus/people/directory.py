"""人际 people：人物档案，按账号建（路线图 1.8，05 号文档第 3 节、第 8 节）。

只认平台账号 ID，不认昵称：没见过的账号就是新的人，不靠名字合并（跨平台关联留到 Phase 3）。
用户的账号来自 identity.toml，由 ensure_user 登记。表 people 和 accounts 只属于这个子系统，
放在 aureus.db 里。
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from aureus.common import Clock
from aureus.common.sqlite import open_connection

_TIMESTAMP_PRECISION = "microseconds"


class PeopleError(Exception):
    """人物档案出了问题。消息是中文。"""


class PersonKind(StrEnum):
    """这个人是谁。"""

    USER = "user"  # 用户本人，只有一个
    CONTACT = "contact"  # 其他人


@dataclass(frozen=True)
class Person:
    """一份人物档案。"""

    id: int
    kind: PersonKind
    # 用户是身份配置里的称呼；其他人认识之前是空的，不去猜。
    name: str
    first_seen: datetime

    @property
    def is_user(self) -> bool:
        """是不是用户本人。"""
        return self.kind is PersonKind.USER


class PeopleDirectory:
    """人物档案：按（平台，账号 ID）找人，没见过的账号自动建档。"""

    def __init__(self, db_path: Path, clock: Clock) -> None:
        self._db_path = db_path
        self._clock = clock
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """打开数据库连接并建表。已经打开时什么也不做。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            self._init_schema(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('user', 'contact')),
                name TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL
            )
            """
        )
        # 数据库层面保证用户只有一个：多一个「用户」，就等于多一个能看到私聊的人。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_user ON people(kind) WHERE kind = 'user'"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                platform TEXT NOT NULL,
                account_id TEXT NOT NULL,
                person_id INTEGER NOT NULL REFERENCES people(id),
                PRIMARY KEY (platform, account_id)
            )
            """
        )
        conn.commit()

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("人物档案还没有打开")
        return self._conn

    def _now(self) -> str:
        return self._clock.now().isoformat(timespec=_TIMESTAMP_PRECISION)

    def _new_person(self, conn: sqlite3.Connection, kind: PersonKind, name: str) -> int:
        cursor = conn.execute(
            "INSERT INTO people (kind, name, first_seen) VALUES (?, ?, ?)",
            (kind.value, name, self._now()),
        )
        if cursor.lastrowid is None:
            raise PeopleError("人物档案没能建出来")
        return cursor.lastrowid

    def _load(self, conn: sqlite3.Connection, person_id: int) -> Person:
        row = conn.execute(
            "SELECT id, kind, name, first_seen FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        return Person(
            id=int(row["id"]),
            kind=PersonKind(row["kind"]),
            name=str(row["name"]),
            first_seen=datetime.fromisoformat(row["first_seen"]),
        )

    def ensure_user(self, name: str, accounts: Sequence[tuple[str, str]]) -> Person:
        """登记用户和用户的账号（来自 identity.toml）。可以反复调用，已有的不重复建。

        Args:
            name: 它怎么称呼用户。
            accounts: 用户的（平台，账号 ID）。

        Raises:
            PeopleError: 某个账号已经登记在别人名下。不自动改过去：账号是认人的唯一依据，
                悄悄换主人，别人就可能被当成用户。
        """
        conn = self._require_open()
        # `with conn` 成功时提交、出错时回滚：账号冲突时，这次登记一条都不留。
        with conn:
            row = conn.execute("SELECT id FROM people WHERE kind = 'user'").fetchone()
            if row is None:
                user_id = self._new_person(conn, PersonKind.USER, name)
            else:
                user_id = int(row["id"])
                conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, user_id))
            for platform, account_id in accounts:
                owner = conn.execute(
                    "SELECT person_id FROM accounts WHERE platform = ? AND account_id = ?",
                    (platform, account_id),
                ).fetchone()
                if owner is None:
                    conn.execute(
                        "INSERT INTO accounts (platform, account_id, person_id) VALUES (?, ?, ?)",
                        (platform, account_id, user_id),
                    )
                elif int(owner["person_id"]) != user_id:
                    raise PeopleError(
                        f"{platform} 平台的账号 {account_id} 已经登记在别人名下，"
                        "不能同时是用户的账号。"
                    )
            return self._load(conn, user_id)

    def find(self, platform: str, account_id: str) -> Person | None:
        """按账号找人；没见过的账号返回 None。"""
        conn = self._require_open()
        row = conn.execute(
            "SELECT person_id FROM accounts WHERE platform = ? AND account_id = ?",
            (platform, account_id),
        ).fetchone()
        return None if row is None else self._load(conn, int(row["person_id"]))

    def get(self, person_id: int) -> Person | None:
        """按人物编号取档案；不存在返回 None，供经历中的称呼查询（D80）。"""
        conn = self._require_open()
        row = conn.execute("SELECT id FROM people WHERE id = ?", (person_id,)).fetchone()
        return self._load(conn, person_id) if row is not None else None

    def meet(self, platform: str, account_id: str) -> Person:
        """按账号找人；没见过的账号建一份新的联系人档案（新账号就是新的人）。"""
        conn = self._require_open()
        with conn:
            row = conn.execute(
                "SELECT person_id FROM accounts WHERE platform = ? AND account_id = ?",
                (platform, account_id),
            ).fetchone()
            if row is not None:
                return self._load(conn, int(row["person_id"]))
            person_id = self._new_person(conn, PersonKind.CONTACT, "")
            conn.execute(
                "INSERT INTO accounts (platform, account_id, person_id) VALUES (?, ?, ?)",
                (platform, account_id, person_id),
            )
            return self._load(conn, person_id)

    def accounts_of(self, person_id: int) -> list[tuple[str, str]]:
        """这个人的全部（平台，账号 ID），按平台和账号排序。"""
        conn = self._require_open()
        rows = conn.execute(
            "SELECT platform, account_id FROM accounts WHERE person_id = ? "
            "ORDER BY platform, account_id",
            (person_id,),
        ).fetchall()
        return [(str(row["platform"]), str(row["account_id"])) for row in rows]
