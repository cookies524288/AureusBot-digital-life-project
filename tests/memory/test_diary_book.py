"""日记文件创建和不覆盖约定（D80）。"""

from datetime import date
from pathlib import Path

import pytest

from aureus.memory import DiaryBook


def test_creates_directory_and_writes_day_without_overwriting(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "diary"
    book = DiaryBook(directory)
    day = date(2026, 11, 20)
    assert book.write(day, "今天自己去看了星星。") == "2026-11-20.md"
    path = directory / "2026-11-20.md"
    assert path.read_text(encoding="utf-8") == "今天自己去看了星星。"
    with pytest.raises(FileExistsError):
        book.write(day, "覆盖")
    assert path.read_text(encoding="utf-8") == "今天自己去看了星星。"
