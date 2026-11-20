"""日记文件：每天一个 UTF-8 文件，已存在时不覆盖（D80 第 7 条）。"""

from datetime import date
from pathlib import Path


class DiaryBook:
    """只负责创建日记；调用方负责器官、时机和时间线。"""

    def __init__(self, diary_dir: Path) -> None:
        self._diary_dir = diary_dir

    def write(self, day: date, text: str) -> str:
        """写 YYYY-MM-DD.md 并返回文件名；已存在抛 FileExistsError，其他错误抛 OSError。"""
        self._diary_dir.mkdir(parents=True, exist_ok=True)
        name = f"{day.isoformat()}.md"
        with (self._diary_dir / name).open("x", encoding="utf-8") as file:
            file.write(text)
        return name
