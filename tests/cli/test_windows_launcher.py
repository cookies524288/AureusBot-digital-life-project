"""Windows 图形启动器的数据根目录选择。"""

from pathlib import Path

import httpx
import pytest

from aureus.cli import windows_launcher
from aureus.common import AureusPaths, HomePathError
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


class RecordingGet:
    """记录健康检查的地址和参数，按需要返回响应或抛出网络错误。"""

    def __init__(self, result: httpx.Response | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, **options: object) -> httpx.Response:
        self.calls.append((url, options))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_open_running_web_opens_healthy_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path)
    monkeypatch.setattr(
        windows_launcher, "running_web_instance", lambda home: ("http://127.0.0.1:18771", 42)
    )
    get = RecordingGet(httpx.Response(200))
    monkeypatch.setattr(windows_launcher.httpx, "get", get)
    opened: list[str] = []
    monkeypatch.setattr(windows_launcher.os, "startfile", opened.append)

    assert windows_launcher._open_running_web(paths, tray=False) is True
    assert opened == ["http://127.0.0.1:18771"]
    assert get.calls == [("http://127.0.0.1:18771", {"trust_env": False, "timeout": 1})]


def test_open_running_web_reports_attach_in_tray_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = AureusPaths(tmp_path)
    monkeypatch.setattr(
        windows_launcher, "running_web_instance", lambda home: ("http://127.0.0.1:18771", 42)
    )
    monkeypatch.setattr(windows_launcher.httpx, "get", RecordingGet(httpx.Response(200)))
    monkeypatch.setattr(windows_launcher.os, "startfile", pytest.fail)

    assert windows_launcher._open_running_web(paths, tray=True) is True
    assert capsys.readouterr().out == "AUREUS_WEB_ATTACH=42 http://127.0.0.1:18771\n"


@pytest.mark.parametrize(
    "result",
    [httpx.Response(503), httpx.ConnectError("拒绝连接"), httpx.ReadTimeout("超时")],
)
def test_open_running_web_skips_unhealthy_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: httpx.Response | Exception,
) -> None:
    paths = AureusPaths(tmp_path)
    monkeypatch.setattr(
        windows_launcher, "running_web_instance", lambda home: ("http://127.0.0.1:18771", 42)
    )
    monkeypatch.setattr(windows_launcher.httpx, "get", RecordingGet(result))
    monkeypatch.setattr(windows_launcher.os, "startfile", pytest.fail)

    assert windows_launcher._open_running_web(paths, tray=False) is False


def test_open_running_web_without_instance_skips_health_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path)
    monkeypatch.setattr(windows_launcher, "running_web_instance", lambda home: None)
    monkeypatch.setattr(windows_launcher.httpx, "get", pytest.fail)

    assert windows_launcher._open_running_web(paths, tray=False) is False


def test_startup_wait_covers_normal_shutdown() -> None:
    assert windows_launcher._STARTUP_WAIT_SECONDS == 180.0
