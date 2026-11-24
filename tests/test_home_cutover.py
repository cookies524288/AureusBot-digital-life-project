"""Windows 默认数据目录切换的离线合成回归。"""

import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from aureus.cli import windows_launcher
from aureus.common import AureusPaths
from aureus.common import paths as common_paths
from aureus.identity import IdentityService
from scripts import check_home_cutover as cutover

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="使用 Windows Robocopy 和目录联接")


@pytest.fixture
def homes(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "active home 合成"
    target = tmp_path / "fixture project 合成" / "aureus_home"
    target.mkdir(parents=True)
    database = AureusPaths(source).database_file
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (entry TEXT)")
        connection.execute("INSERT INTO sample VALUES ('before')")
        connection.commit()
    return source, target


def test_copy_checks_latest_stopped_home_and_launcher_fallback(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = homes
    project = target.parent
    pointer = project / "aureus-home.txt"
    (project / "pyproject.toml").touch()
    pointer.write_text(f"{source}\n", encoding="utf-8-sig")
    monkeypatch.setattr(windows_launcher, "PROJECT_ROOT", project)
    monkeypatch.setattr(common_paths, "PROJECT_ROOT", project)
    monkeypatch.setattr(common_paths, "PROJECT_HOME", target)
    monkeypatch.setenv("AUREUS_HOME", str(source))
    pointer.rename(project / "aureus-home.paused.txt")
    assert windows_launcher._launcher_home() == source
    (project / "aureus-home.paused.txt").rename(pointer)
    monkeypatch.delenv("AUREUS_HOME")
    assert windows_launcher._launcher_home() == source

    paths = AureusPaths(source)
    paths.config_dir.mkdir()
    media = source / "data" / "memory" / "media" / "nested"
    media.mkdir(parents=True)
    (media / "empty").mkdir()
    files = {
        paths.runtime_config_file: b"[web]\nport = 28976\n",
        paths.identity_file: b"synthetic identity only\n",
        paths.env_file: b"not a credential\n",
        paths.vault_file: b"synthetic encrypted fixture\n",
        paths.master_key_file: b"synthetic key fixture\n",
        media / "sample.bin": bytes(range(64)),
        media / cutover.LOCK_NAME: b"ordinary nested file\n",
    }
    for path, content in files.items():
        path.write_bytes(content)
    subprocess.run(["attrib.exe", "+H", str(paths.env_file)], check=True)

    with IdentityService(paths).acquire_lock(), closing(sqlite3.connect(paths.database_file)) as db:
        db.execute("INSERT INTO sample VALUES ('latest')")
        db.commit()
        assert paths.database_file.with_name("aureus.db-wal").stat().st_size > 0
        with pytest.raises(cutover.CutoverError, match="单实例锁"):
            cutover.copy_checked_home(source, target, confirmed_stopped=True)
        assert not list(target.iterdir())
        assert windows_launcher._launcher_home() == source

    marker = source / cutover.LOCK_NAME
    marker.write_bytes(b"existing offline lock marker")
    assert cutover.copy_checked_home(source, target, confirmed_stopped=True) == len(files) + 1
    assert marker.read_bytes() == b"existing offline lock marker"
    assert not (target / cutover.LOCK_NAME).exists()
    assert (target / (media / "empty").relative_to(source)).is_dir()
    for path, content in files.items():
        assert path.read_bytes() == (target / path.relative_to(source)).read_bytes() == content
    attribute = (target / ".env").stat().st_file_attributes
    assert attribute & stat.FILE_ATTRIBUTE_HIDDEN
    copied_db = AureusPaths(target).database_file
    with closing(sqlite3.connect(f"{copied_db.as_uri()}?mode=ro", uri=True)) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert copy.execute("SELECT entry FROM sample ORDER BY rowid").fetchall() == [
            ("before",),
            ("latest",),
        ]
    assert windows_launcher._launcher_home() == source
    backup = pointer.rename(project / "aureus-home.saved.txt")
    assert windows_launcher._launcher_home() == target
    backup.rename(pointer)
    assert windows_launcher._launcher_home() == source


def test_refuses_without_explicit_shutdown_confirmation(homes: tuple[Path, Path]) -> None:
    source, target = homes
    with pytest.raises(cutover.CutoverError, match="--confirm-stopped"):
        cutover.copy_checked_home(source, target, confirmed_stopped=False)
    assert not list(target.iterdir())
    assert not (source / cutover.LOCK_NAME).exists()


@pytest.mark.parametrize("existing", ["empty child", "hidden file"])
def test_refuses_nonempty_target(homes: tuple[Path, Path], existing: str) -> None:
    source, target = homes
    entry = target / existing
    if existing == "empty child":
        entry.mkdir()
    else:
        entry.write_bytes(b"keep existing data")
        subprocess.run(["attrib.exe", "+H", str(entry)], check=True)
    with pytest.raises(cutover.CutoverError, match="完全为空"):
        cutover.copy_checked_home(source, target, confirmed_stopped=True)
    assert set(target.iterdir()) == {entry}
    if existing == "hidden file":
        assert entry.read_bytes() == b"keep existing data"


@pytest.mark.parametrize("overlap", ["equal", "inside_source", "inside_target"])
def test_refuses_overlapping_roots(homes: tuple[Path, Path], overlap: str) -> None:
    source, target = homes
    if overlap == "equal":
        target = source
    elif overlap == "inside_source":
        target = source / "aureus_home"
        target.mkdir()
    else:
        source.rename(target / "source")
        source = target / "source"
    with pytest.raises(cutover.CutoverError, match="互相包含"):
        cutover.copy_checked_home(source, target, confirmed_stopped=True)
    assert not (source / cutover.LOCK_NAME).exists()


@pytest.mark.parametrize(
    "placement", ["source_child", "source_parent", "source_lock", "target_root"]
)
def test_refuses_junctions(homes: tuple[Path, Path], placement: str) -> None:
    source, target = homes
    outside = target.parent / "outside"
    outside.mkdir()
    if placement == "source_child":
        junction = source / "nested link"
    elif placement == "source_parent":
        junction = target.parent / "linked parent"
        outside = source.parent
        source = junction / source.name
    elif placement == "source_lock":
        junction = source / cutover.LOCK_NAME
    else:
        target.rmdir()
        junction = target
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)], check=True)
    try:
        with pytest.raises(cutover.CutoverError, match="重解析点"):
            cutover.copy_checked_home(source, target, confirmed_stopped=True)
        assert not list(target.iterdir())
    finally:
        junction.rmdir()


def test_rejects_same_size_file_corruption(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = homes
    (source / "synthetic.bin").write_bytes(b"original")
    pointer = target.parent / "aureus-home.txt"
    pointer.write_text(str(source), encoding="utf-8")
    robocopy = subprocess.run

    def copy_with_corruption(
        command: list[str], *, check: bool, stdout: int, stderr: int
    ) -> subprocess.CompletedProcess[bytes]:
        result = robocopy(command, check=check, stdout=stdout, stderr=stderr)
        (target / "synthetic.bin").write_bytes(b"ticigano")
        return result

    monkeypatch.setattr(cutover.subprocess, "run", copy_with_corruption)
    with pytest.raises(cutover.CutoverError, match="文件内容校验不一致"):
        cutover.copy_checked_home(source, target, confirmed_stopped=True)
    assert (source / "synthetic.bin").read_bytes() == b"original"
    assert (target / "synthetic.bin").read_bytes() == b"ticigano"
    assert pointer.read_text(encoding="utf-8") == str(source)
