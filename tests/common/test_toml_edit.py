"""TOML 写回：保留原文，失败不能留下半份文件（D84）。"""

import os
import tomllib
from pathlib import Path
from typing import Any

import pytest
from tomlkit.toml_document import TOMLDocument

from aureus.common import ConfigError, apply_toml_edit

ORIGINAL = '# 用户的注释\nlog_level = "INFO"  # 日志\n\n[diary]\n# 保留\nwrite_after_hour = 22\n'


def edit_hour(document: TOMLDocument) -> None:
    document["diary"]["write_after_hour"] = 21


def validate_hour(raw: dict[str, Any]) -> None:
    assert type(raw["diary"]["write_after_hour"]) is int
    assert raw["diary"]["write_after_hour"] == 21


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_edit_keeps_comments_other_fields_and_line_endings(tmp_path: Path, newline: str) -> None:
    path = tmp_path / "config.toml"
    original = ORIGINAL.replace("\n", newline).encode("utf-8")
    path.write_bytes(original)
    apply_toml_edit(path, edit_hour, validate_hour)
    assert path.read_bytes() == original.replace(b"22", b"21")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


@pytest.mark.parametrize("failure", ["edit", "validate", "fsync", "write", "replace"])
def test_failure_keeps_original_bytes_and_removes_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(ORIGINAL.encode("utf-8"))

    def broken(*args: Any, **kwargs: Any) -> None:
        if failure in ("edit", "validate"):
            raise ConfigError("不合格")
        raise OSError("模拟磁盘故障")

    if failure in ("fsync", "replace"):
        monkeypatch.setattr(os, failure, broken)
    if failure == "write":
        original_fdopen = os.fdopen

        def failing_file(*args: Any, **kwargs: Any) -> Any:
            file = original_fdopen(*args, **kwargs)
            monkeypatch.setattr(file, "write", broken)
            return file

        monkeypatch.setattr(os, "fdopen", failing_file)
    with pytest.raises((ConfigError, OSError)):
        apply_toml_edit(
            path,
            broken if failure == "edit" else edit_hour,
            broken if failure == "validate" else validate_hour,
        )
    assert path.read_bytes() == ORIGINAL.encode("utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_missing_file_is_created_only_after_validation(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.toml"

    def edit(document: TOMLDocument) -> None:
        document["log_level"] = "DEBUG"

    def validate(raw: dict[str, Any]) -> None:
        assert raw == {"log_level": "DEBUG"}

    apply_toml_edit(path, edit, validate)
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {"log_level": "DEBUG"}
    assert b"\r\n" not in path.read_bytes()
