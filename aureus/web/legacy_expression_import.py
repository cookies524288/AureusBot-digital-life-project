"""MaiBot 062d45c 旧表达库投影（GPL-3.0），仅解析上传的副本。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from aureus.memory import LanguageContent
from aureus.memory.language import LanguagePackageRow

MAX_LEGACY_BYTES = 32 * 1024 * 1024
_MAX_ROWS = 10000


def read_legacy_expressions(data: bytes) -> dict[str, list[LanguagePackageRow]]:
    """只读旧版表达实表，不返回聊天原文或读取外部文件。"""
    if not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("请选择有效的旧版 SQLite 数据库。")
    groups: dict[str, list[LanguagePackageRow]] = {}
    with TemporaryDirectory(prefix="aureus-legacy-expression-") as directory:
        path = Path(directory) / "upload.db"
        path.write_bytes(data)
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            tables = {
                row[1] for row in connection.execute("PRAGMA table_list") if row[2] == "table"
            }
            table = "expression" if "expression" in tables else "expressions"
            if table not in tables:
                raise ValueError("上传库没有旧版表达表。")
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            scope = next((name for name in ("chat_id", "session_id") if name in columns), None)
            if scope is None or not {"style", "situation"} <= columns:
                raise ValueError("旧版表达表缺少必要字段。")
            count = '"count"' if "count" in columns else "0"
            indicators = [
                f'coalesce("{field}", 0) != 1'
                for field in ("disabled", "deleted", "is_deleted")
                if field in columns
            ]
            condition = " AND ".join(indicators) or "1"
            rows = connection.execute(
                f'SELECT substr("{scope}", 1, 4097), substr(style, 1, 81), '
                f"substr(situation, 1, 161), {count} FROM {table} WHERE {condition} LIMIT ?",
                (_MAX_ROWS + 1,),
            )
            for number, (group, style, situation, occurrences) in enumerate(rows, 1):
                if number > _MAX_ROWS:
                    raise ValueError("旧版表达记录超过导入上限。")
                if not all(isinstance(value, str) for value in (group, style, situation)):
                    continue
                if not group or len(group) > 4096:
                    continue
                try:
                    content = LanguageContent(style, situation).validated()
                except ValueError:
                    continue
                groups.setdefault(group, []).append(
                    LanguagePackageRow(
                        content,
                        max(0, occurrences) if type(occurrences) is int else 0,
                        "旧版表达",
                        json.dumps(
                            {
                                "style": content.text,
                                "situation": content.detail,
                                "old_chat_id": group,
                            }
                        ),
                    )
                )
    if not groups:
        raise ValueError("没有可预览导入的表达。")
    return groups
