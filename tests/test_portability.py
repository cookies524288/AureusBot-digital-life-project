"""可移植性的测试（D38，验收标准见 docs/14-user-data.md 第 6.5 节）。

三件事：
1. 所有写入都落在数据根目录之内，不往当前目录、系统临时目录等别处写；
2. 整个数据根目录搬到别处之后仍能读取，并且没有任何文件存着旧位置的绝对路径；
3. 代码里没有写死的盘符。
"""

import ast
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from aureus.common import AureusPaths, configure_logging, shutdown_logging
from aureus.common.vault import SecretVault
from aureus.identity import IdentityService
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery

# 全部是编造的假密钥，不是真的。
FAKE_NAME = "AUREUS_KEY_FAKE"
FAKE_SECRET = "sk-fake-portability-0001"
RECORD_CONTENT = "搬家前的一条记录"

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "aureus"

# 盘符加分隔符，比如 C:\ 或 D:/。前面不能紧挨字母和数字，
# 免得把 http:// 里的 p:/ 或 "a:b" 这样的普通文字当成盘符。
DRIVE_LETTER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")


def populate_home(paths: AureusPaths) -> None:
    """走一遍所有会写文件的子系统，让数据根目录里有一份有代表性的数据。"""
    paths.ensure_directories()
    SecretVault(paths.vault_file, paths.master_key_file).set(FAKE_NAME, FAKE_SECRET)
    database = TimelineDatabase(paths.database_file)
    database.open()
    try:
        database.append("test", RECORD_CONTENT, ActorType.ME)
    finally:
        database.close()  # Windows 上不关就搬不动数据库文件
    with IdentityService(paths).acquire_lock():
        pass
    configure_logging(paths.log_dir, "INFO", to_console=False)
    logger.info("搬家前的一条日志")
    shutdown_logging()  # 先关掉，保证排队的日志都已经写进文件，日志文件也不再被占用


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


def test_all_writes_stay_inside_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working_dir = tmp_path / "working_dir"
    system_temp = tmp_path / "system_temp"
    working_dir.mkdir()
    system_temp.mkdir()
    # 把“当前目录”和“系统临时目录”都换成空文件夹：只要有代码往那里写，就会被发现。
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))

    populate_home(AureusPaths(tmp_path / "home"))

    assert list(working_dir.iterdir()) == []
    assert list(system_temp.iterdir()) == []
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "home",
        "system_temp",
        "working_dir",
    ]


def test_moved_data_root_is_still_readable(tmp_path: Path) -> None:
    old_home = tmp_path / "old_disk" / "aureus_home"
    new_home = tmp_path / "new_disk" / "moved_home"
    populate_home(AureusPaths(old_home))
    new_home.parent.mkdir()

    shutil.move(old_home, new_home)

    assert not old_home.exists()
    moved = AureusPaths(new_home)
    assert SecretVault(moved.vault_file, moved.master_key_file).load() == {FAKE_NAME: FAKE_SECRET}
    database = TimelineDatabase(moved.database_file)
    database.open()
    try:
        records = database.query(TimelineQuery())
    finally:
        database.close()
    assert [record.content for record in records] == [RECORD_CONTENT]
    # 单实例锁也跟着数据根目录走：搬到新位置后照样能拿到。
    with IdentityService(moved).acquire_lock():
        pass


def test_no_file_stores_the_absolute_location(tmp_path: Path) -> None:
    # 文件夹名取一个不会撞上别的内容的怪名字：
    # 任何文件里出现它，就说明有代码把绝对路径存了下来，搬家后会指向旧位置。
    marker = "distinctive-location-name-for-portability"
    home = tmp_path / marker / "home"
    populate_home(AureusPaths(home))

    stored_files = [path for path in home.rglob("*") if path.is_file()]
    assert stored_files, "没有产生任何文件，这个测试就失去意义了"
    leaking = [
        path.relative_to(home)
        for path in stored_files
        if marker.encode("utf-8") in path.read_bytes()
    ]
    assert leaking == []


@pytest.mark.parametrize("text", ["C:\\Users\\someone", "D:/data/aureus", "see E:\\x"])
def test_drive_letter_pattern_catches_hard_coded_paths(text: str) -> None:
    assert DRIVE_LETTER_PATTERN.search(text)


@pytest.mark.parametrize(
    "text", ["http://example.com", "key:value", "a:b", "https://example.com:8080/x"]
)
def test_drive_letter_pattern_ignores_ordinary_text(text: str) -> None:
    assert DRIVE_LETTER_PATTERN.search(text) is None


def string_constants(source: str) -> Iterator[str]:
    """取出源代码里所有字符串字面量（含 f-string 里写死的部分），不含注释。"""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def test_source_has_no_hard_coded_drive_letters() -> None:
    offenders = [
        f"{path.relative_to(SOURCE_ROOT.parent)}: {text[:60]!r}"
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for text in string_constants(path.read_text(encoding="utf-8"))
        if DRIVE_LETTER_PATTERN.search(text)
    ]
    assert offenders == []
