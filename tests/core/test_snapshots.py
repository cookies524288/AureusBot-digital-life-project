# 快照大小上限的测试：用假文件和手工设置的修改时间，不碰真实数据。
import os
from pathlib import Path

import pytest

from aureus.common import AureusPaths, load_runtime_config
from aureus.core.snapshots import BYTES_PER_MEGABYTE, prune_snapshots


def make_snapshot(folder: Path, name: str, size: int, age_rank: int) -> Path:
    """age_rank 越大越新。"""
    path = folder / name
    path.write_bytes(b"x" * size)
    os.utime(path, (1_000_000 + age_rank, 1_000_000 + age_rank))
    return path


def test_removes_oldest_until_under_limit(tmp_path: Path) -> None:
    oldest = make_snapshot(tmp_path, "a.zip", 100, 1)
    middle = make_snapshot(tmp_path, "b.zip", 100, 2)
    newest = make_snapshot(tmp_path, "c.zip", 100, 3)
    removed = prune_snapshots(tmp_path, 250)
    assert removed == [oldest]
    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()


def test_under_limit_removes_nothing(tmp_path: Path) -> None:
    make_snapshot(tmp_path, "a.zip", 100, 1)
    make_snapshot(tmp_path, "b.zip", 100, 2)
    assert prune_snapshots(tmp_path, 200) == []


def test_newest_snapshot_is_always_kept_even_if_over_limit(tmp_path: Path) -> None:
    old = make_snapshot(tmp_path, "old.zip", 50, 1)
    newest = make_snapshot(tmp_path, "new.zip", 500, 2)
    removed = prune_snapshots(tmp_path, 10)
    assert removed == [old]
    assert newest.exists()


def test_other_files_and_subfolders_are_untouched(tmp_path: Path) -> None:
    make_snapshot(tmp_path, "a.zip", 100, 1)
    make_snapshot(tmp_path, "b.zip", 100, 2)
    note = tmp_path / "notes.txt"
    note.write_bytes(b"y" * 1000)
    nested = tmp_path / "nested"
    nested.mkdir()
    inner = make_snapshot(nested, "inner.zip", 1000, 0)
    prune_snapshots(tmp_path, 1)
    assert note.exists()
    assert inner.exists()


def test_missing_folder_is_fine(tmp_path: Path) -> None:
    assert prune_snapshots(tmp_path / "nope", 1) == []


def test_undeletable_file_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = make_snapshot(tmp_path, "a.zip", 100, 1)
    other = make_snapshot(tmp_path, "b.zip", 100, 2)
    newest = make_snapshot(tmp_path, "c.zip", 100, 3)
    real_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == stuck:
            raise PermissionError("被占用")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    removed = prune_snapshots(tmp_path, 150)
    assert removed == [other]
    assert stuck.exists()
    assert newest.exists()


def test_config_default_and_validation(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    paths.ensure_directories()
    assert load_runtime_config(paths).snapshots.max_total_megabytes == 1024
    paths.runtime_config_file.write_text("[snapshots]\nmax_total_megabytes = 5\n", encoding="utf-8")
    assert load_runtime_config(paths).snapshots.max_total_megabytes == 5
    assert BYTES_PER_MEGABYTE == 1024 * 1024
