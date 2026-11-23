"""调试重启入口只打开已发布地址，不启动实例或发重启请求。"""

from pathlib import Path

import pytest

from aureus.cli import debug_restart
from aureus.common.paths import HomePathError


def test_debug_restart_opens_only_published_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(debug_restart, "_launcher_home", lambda: tmp_path)
    monkeypatch.setattr(debug_restart, "running_web_url", lambda home: "http://127.0.0.1:29501")
    monkeypatch.setattr(debug_restart.webbrowser, "open", lambda url: opened.append(url) or True)
    assert debug_restart.main() == 0
    assert opened == ["http://127.0.0.1:29501"]
    assert "尚未重启" in capsys.readouterr().out


def test_debug_restart_never_guesses_or_starts_missing_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(debug_restart, "_launcher_home", lambda: tmp_path)
    monkeypatch.setattr(debug_restart, "running_web_url", lambda home: None)
    monkeypatch.setattr(debug_restart.webbrowser, "open", lambda url: opened.append(url))
    assert debug_restart.main() == 1
    assert opened == []
    assert "未启动实例，也未重启" in capsys.readouterr().out


def test_debug_restart_invalid_home_and_browser_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid_home() -> Path:
        raise HomePathError("合成数据目录无效")

    monkeypatch.setattr(debug_restart, "_launcher_home", invalid_home)
    assert debug_restart.main() == 1
    assert "合成数据目录无效" in capsys.readouterr().out
    monkeypatch.setattr(debug_restart, "_launcher_home", lambda: tmp_path)
    monkeypatch.setattr(debug_restart, "running_web_url", lambda home: "http://127.0.0.1:29501")
    monkeypatch.setattr(debug_restart.webbrowser, "open", lambda url: False)
    assert debug_restart.main() == 1
    assert "未发出重启请求" in capsys.readouterr().out
