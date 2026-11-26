"""Windows 图形启动器的数据根目录选择、健康检查和残留实例接管。"""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from aureus.cli import windows_launcher
from aureus.common import AureusPaths, HomePathError
from aureus.common import paths as common_paths
from aureus.identity import SingleInstanceLockError


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


LOCK_PID = 1234
LOCKED_AT = datetime(2026, 11, 26, 7, 0, tzinfo=UTC)
CREATED_AT = "2026-11-26T06:00:00.0000000Z"
LAUNCHER_COMMAND = "pythonw.exe -m aureus.cli.windows_launcher --tray --owner-pid 42"


def write_pid_file(paths: AureusPaths, content: str) -> Path:
    """写 PID 标记并把修改时间设为 LOCKED_AT，晚于测试进程的创建时间。"""
    paths.home.mkdir(parents=True, exist_ok=True)
    pid_file = paths.home / "aureus.lock.pid"
    pid_file.write_text(content, encoding="utf-8")
    timestamp = LOCKED_AT.timestamp()
    os.utime(pid_file, (timestamp, timestamp))
    return pid_file


def launcher_process(command_line: object = LAUNCHER_COMMAND) -> dict[str, object]:
    return {"CommandLine": command_line, "Started": CREATED_AT}


class RecordingRun:
    """subprocess.run 的替身：回答进程查询，记录 taskkill 的参数。"""

    def __init__(self, process: dict[str, object] | None, *, taskkill_code: int = 0) -> None:
        self.process = process
        self.taskkill_code = taskkill_code
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, options))
        if args[0] == "taskkill":
            return subprocess.CompletedProcess(args, self.taskkill_code)
        records = [self.process] if self.process is not None else []
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(records))


def patch_process_query(
    monkeypatch: pytest.MonkeyPatch, process: dict[str, object] | None, *, taskkill_code: int = 0
) -> RecordingRun:
    run = RecordingRun(process, taskkill_code=taskkill_code)
    monkeypatch.setattr(windows_launcher.subprocess, "run", run)
    monkeypatch.setattr(windows_launcher, "_confirm_takeover", pytest.fail)
    return run


def test_takeover_rejects_missing_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = AureusPaths(tmp_path)
    run = patch_process_query(monkeypatch, launcher_process())

    assert windows_launcher._takeover_stale_instance(paths) is False
    assert run.calls == []


@pytest.mark.parametrize("content", ["", "not-a-pid", "0", "-5", "12.5"])
def test_takeover_rejects_invalid_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    paths = AureusPaths(tmp_path)
    write_pid_file(paths, content)
    run = patch_process_query(monkeypatch, launcher_process())

    assert windows_launcher._takeover_stale_instance(paths) is False
    assert run.calls == []


def test_takeover_rejects_missing_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = AureusPaths(tmp_path)
    write_pid_file(paths, str(LOCK_PID))
    run = patch_process_query(monkeypatch, None)

    assert windows_launcher._takeover_stale_instance(paths) is False
    assert [args[0] for args, _ in run.calls] == ["powershell.exe"]


@pytest.mark.parametrize(
    "command_line",
    [
        "pythonw.exe -m aureus.cli.something_else",
        "pythonw.exe -m aureus.cli.windows_launcher_extra",
        None,
    ],
)
def test_takeover_rejects_another_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_line: object
) -> None:
    paths = AureusPaths(tmp_path)
    write_pid_file(paths, str(LOCK_PID))
    run = patch_process_query(monkeypatch, launcher_process(command_line))

    assert windows_launcher._takeover_stale_instance(paths) is False
    assert [args[0] for args, _ in run.calls] == ["powershell.exe"]


@pytest.mark.parametrize(
    "started", ["2026-11-26T08:00:00.0000000Z", "2026-11-26T07:00:00.0000000Z"]
)
def test_takeover_rejects_a_process_created_after_the_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: str
) -> None:
    paths = AureusPaths(tmp_path)
    write_pid_file(paths, str(LOCK_PID))
    run = patch_process_query(monkeypatch, {"CommandLine": LAUNCHER_COMMAND, "Started": started})

    assert windows_launcher._takeover_stale_instance(paths) is False
    assert [args[0] for args, _ in run.calls] == ["powershell.exe"]


def test_takeover_does_nothing_when_maintainer_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path)
    write_pid_file(paths, str(LOCK_PID))
    run = patch_process_query(monkeypatch, launcher_process())
    confirmed: list[int] = []
    monkeypatch.setattr(
        windows_launcher, "_confirm_takeover", lambda pid: confirmed.append(pid) or False
    )
    monkeypatch.setattr(windows_launcher, "sleep", pytest.fail)

    assert windows_launcher._takeover_stale_instance(paths) is False
    assert confirmed == [LOCK_PID]
    assert [args[0] for args, _ in run.calls] == ["powershell.exe"]


def test_takeover_returns_false_when_taskkill_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path)
    write_pid_file(paths, str(LOCK_PID))
    run = patch_process_query(monkeypatch, launcher_process(), taskkill_code=1)
    monkeypatch.setattr(windows_launcher, "_confirm_takeover", lambda pid: True)
    monkeypatch.setattr(windows_launcher, "sleep", pytest.fail)

    assert windows_launcher._takeover_stale_instance(paths) is False
    assert [args[0] for args, _ in run.calls] == ["powershell.exe", "taskkill"]


def test_takeover_ends_the_verified_process_after_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path)
    write_pid_file(paths, str(LOCK_PID))
    run = patch_process_query(monkeypatch, launcher_process())
    monkeypatch.setattr(windows_launcher, "_confirm_takeover", lambda pid: True)
    sleep_calls: list[float] = []
    monkeypatch.setattr(windows_launcher, "sleep", sleep_calls.append)

    assert windows_launcher._takeover_stale_instance(paths) is True
    assert sleep_calls == [1.0]
    taskkill_args, taskkill_options = run.calls[1]
    assert taskkill_args == ["taskkill", "/T", "/F", "/PID", str(LOCK_PID)]
    assert taskkill_options == {
        "capture_output": True,
        "timeout": 10,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def test_launch_raises_lock_error_when_takeover_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path)
    monkeypatch.setattr(windows_launcher, "_open_running_web", lambda paths, *, tray: False)

    def busy(*args: object, **kwargs: object) -> None:
        raise SingleInstanceLockError("另一个 Aureus 实例正在运行。")

    monkeypatch.setattr(windows_launcher, "open_core", busy)
    times = iter([0.0, 180.0])
    monkeypatch.setattr(windows_launcher, "monotonic", lambda: next(times))
    monkeypatch.setattr(windows_launcher, "sleep", pytest.fail)
    takeover_calls: list[AureusPaths] = []
    monkeypatch.setattr(
        windows_launcher,
        "_takeover_stale_instance",
        lambda paths: takeover_calls.append(paths) or False,
    )

    with pytest.raises(SingleInstanceLockError):
        windows_launcher._launch(paths, fake=True, tray=True, opening_browser=False, owner_pid=None)
    assert takeover_calls == [paths]


def test_launch_continues_after_a_successful_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path)
    web_checks = iter([False, True])
    monkeypatch.setattr(
        windows_launcher, "_open_running_web", lambda paths, *, tray: next(web_checks)
    )

    def busy(*args: object, **kwargs: object) -> None:
        raise SingleInstanceLockError("另一个 Aureus 实例正在运行。")

    monkeypatch.setattr(windows_launcher, "open_core", busy)
    times = iter([0.0, 180.0, 180.0])
    monkeypatch.setattr(windows_launcher, "monotonic", lambda: next(times))
    monkeypatch.setattr(windows_launcher, "sleep", pytest.fail)
    takeover_calls: list[AureusPaths] = []
    monkeypatch.setattr(
        windows_launcher,
        "_takeover_stale_instance",
        lambda paths: takeover_calls.append(paths) or True,
    )

    assert (
        windows_launcher._launch(paths, fake=True, tray=True, opening_browser=False, owner_pid=None)
        is False
    )
    assert takeover_calls == [paths]
