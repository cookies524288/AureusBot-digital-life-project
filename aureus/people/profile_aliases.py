"""人际 people：按不可变画像来源键保存本机人工别名。

normalize_aliases 移植自 A_Memorix 2.0.0（AGPL-3.0）的 metadata_profile.py：
https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/storage/metadata_profile.py
参数校验及去重规则不变，存储与刷新由 Aureus 自行决定。
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import cast

_MAX_ALIAS_CHARS = 128
_MAX_ALIASES = 100


@dataclass(frozen=True)
class ProfileAliases:
    """既有画像别名、本机维护及按权限读取时的有效结果。"""

    original_aliases: list[str]
    local_aliases: list[str] | None

    @property
    def effective_aliases(self) -> list[str]:
        """本机集合覆盖既有别名，不影响画像其他字段。"""
        return self.original_aliases if self.local_aliases is None else self.local_aliases


def normalize_aliases(aliases: list[str]) -> list[str]:
    """校验并按大小写无关规则去重人工别名。"""
    if not isinstance(aliases, list):
        raise ValueError("aliases 必须是字符串列表")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_alias in aliases:
        if not isinstance(raw_alias, str):
            raise ValueError("aliases 中的每一项都必须是字符串")
        alias = raw_alias.strip()
        if not alias:
            continue
        if len(alias) > _MAX_ALIAS_CHARS:
            raise ValueError(f"单个别名不能超过 {_MAX_ALIAS_CHARS} 个字符")
        key = alias.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(alias)

    if not normalized:
        raise ValueError("至少需要保留一个人物别名")
    if len(normalized) > _MAX_ALIASES:
        raise ValueError(f"人物别名不能超过 {_MAX_ALIASES} 个")
    return normalized


def init_schema(conn: sqlite3.Connection) -> None:
    """建立独立维护层，来源键不受本地人物编号合并影响。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS local_profile_aliases (
            instance TEXT NOT NULL,
            person_key TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            PRIMARY KEY (instance, person_key),
            FOREIGN KEY (instance, person_key) REFERENCES historical_person_keys
        )
    """)


def for_person(conn: sqlite3.Connection, person_id: int) -> dict[tuple[str, str], list[str]]:
    """只按该人物已有的来源键读取，不通过别名查找人物。"""
    rows = conn.execute(
        """SELECT local.instance, local.person_key, local.aliases_json
           FROM local_profile_aliases local JOIN historical_person_keys USING (instance, person_key)
           WHERE historical_person_keys.person_id = ?""",
        (person_id,),
    )
    return {
        (row["instance"], row["person_key"]): cast(list[str], json.loads(row["aliases_json"]))
        for row in rows
    }


def write(conn: sqlite3.Connection, source: tuple[str, str], aliases: list[str] | None) -> None:
    """在调用方的事务内保存完整集合，或仅撤销本机维护。"""
    if aliases is None:
        conn.execute(
            "DELETE FROM local_profile_aliases WHERE instance = ? AND person_key = ?", source
        )
        return
    conn.execute(
        """INSERT INTO local_profile_aliases VALUES (?, ?, ?)
           ON CONFLICT(instance, person_key) DO UPDATE SET aliases_json = excluded.aliases_json""",
        (*source, json.dumps(aliases, ensure_ascii=False)),
    )
