"""Windows 图形启动器的数据根目录选择。"""

from pathlib import Path

import pytest

from aureus.cli import windows_launcher
from aureus.common import HomePathError
from aureus.common import paths as common_paths


def test_launcher_uses_env_then_project_pointer_then_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = tmp_path / "aureus_home"
    migrated = tmp_path / "已迁入"
    override = tmp_path / "override"
    pointer = tmp_path / "aureus-home.txt"
    monkeypatch.setattr(windows_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(common_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(common_paths, "PROJECT_HOME", default)
    monkeypatch.delenv("AUREUS_HOME", raising=False)
    (tmp_path / "pyproject.toml").touch()
    assert windows_launcher._launcher_home() == default

    migrated.mkdir()
    pointer.write_text(f"{migrated}\n", encoding="utf-8-sig")
    assert windows_launcher._launcher_home() == migrated
    assert common_paths.resolve_home() == default

    monkeypatch.setenv("AUREUS_HOME", str(override))
    assert windows_launcher._launcher_home() == override


@pytest.mark.parametrize("target", ["", "relative", "missing"])
def test_invalid_project_pointer_reports_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    monkeypatch.setattr(windows_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("AUREUS_HOME", raising=False)
    pointer = tmp_path / "aureus-home.txt"
    pointer.write_text(str(tmp_path / target) if target == "missing" else target, encoding="utf-8")

    with pytest.raises(HomePathError, match=r"aureus-home\.txt"):
        windows_launcher._launcher_home()


def test_non_utf8_project_pointer_reports_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("AUREUS_HOME", raising=False)
    (tmp_path / "aureus-home.txt").write_bytes(b"\xff")

    with pytest.raises(HomePathError, match="UTF-8"):
        windows_launcher._launcher_home()
