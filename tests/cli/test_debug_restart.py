"""调试重启入口只打开已发布地址，不启动实例或发重启请求。"""

import json
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from typing import Any

import httpx
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
    assert debug_restart.main([]) == 0
    assert opened == ["http://127.0.0.1:29501"]
    assert "尚未重启" in capsys.readouterr().out


def test_debug_restart_never_guesses_or_starts_missing_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(debug_restart, "_launcher_home", lambda: tmp_path)
    monkeypatch.setattr(debug_restart, "running_web_url", lambda home: None)
    monkeypatch.setattr(debug_restart.webbrowser, "open", lambda url: opened.append(url))
    assert debug_restart.main([]) == 1
    assert opened == []
    assert "未启动实例，也未重启" in capsys.readouterr().out


def test_debug_restart_invalid_home_and_browser_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid_home() -> Path:
        raise HomePathError("合成数据目录无效")

    monkeypatch.setattr(debug_restart, "_launcher_home", invalid_home)
    assert debug_restart.main([]) == 1
    assert "合成数据目录无效" in capsys.readouterr().out
    monkeypatch.setattr(debug_restart, "_launcher_home", lambda: tmp_path)
    monkeypatch.setattr(debug_restart, "running_web_url", lambda home: "http://127.0.0.1:29501")
    monkeypatch.setattr(debug_restart.webbrowser, "open", lambda url: False)
    assert debug_restart.main([]) == 1
    assert "未发出重启请求" in capsys.readouterr().out


class FakeClient:
    def __init__(self, *, base_url: str, **kwargs: Any) -> None:
        if kwargs:
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
        self.base_url = base_url
        self.posts: list[str] = []
        self.login_status = 200
        self.instance = "old"
        self.response_instance = "old"

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        self.posts.append(path)
        if path == "/api/login":
            assert kwargs["json"]["password"] == "fake-secret"
            return httpx.Response(self.login_status, json={"must_change_password": False})
        assert path == "/api/restart" and kwargs["headers"] == {"Origin": self.base_url}
        return httpx.Response(200, json={"instance": self.response_instance})

    def get(self, path: str) -> httpx.Response:
        if path == "/api/status":
            return httpx.Response(200, json={"restart_instance": self.instance})
        return httpx.Response(200)


def _fake_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[FakeClient, list[tuple[str, debug_restart.ListenerIdentity] | None]]:
    target = (
        "http://127.0.0.1:29501",
        debug_restart.ListenerIdentity(42, "2026-01-01", 44, "2026-01-02"),
    )
    current: list[tuple[str, debug_restart.ListenerIdentity] | None] = [target]
    client = FakeClient(base_url=target[0])
    monkeypatch.setattr(debug_restart, "_verified_target", lambda home: current[0])
    monkeypatch.setattr(debug_restart, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(debug_restart, "running_web_instance", lambda home: (target[0], 42))
    monkeypatch.setattr(debug_restart, "_listener_identity", lambda port, owner: target[1])
    monkeypatch.setattr(debug_restart, "_old_listener_exited", lambda listener: True)
    monkeypatch.setattr(debug_restart.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(debug_restart.getpass, "getpass", lambda label: "fake-secret")
    monkeypatch.setattr("builtins.input", lambda label: "yes")
    return client, current


def test_interactive_restart_cancel_and_bad_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _current = _fake_restart(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda label: "no")
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == []
    monkeypatch.setattr("builtins.input", lambda label: "yes")
    client.login_status = 401
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == ["/api/login"]


def test_interactive_restart_target_change_before_password_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, current = _fake_restart(monkeypatch, tmp_path)

    def change_target(label: str) -> str:
        current[0] = (
            "http://127.0.0.1:29502",
            debug_restart.ListenerIdentity(77, "2026-01-01", 78, "2026-01-02"),
        )
        return "fake-secret"

    monkeypatch.setattr(debug_restart.getpass, "getpass", change_target)
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == []


def test_interactive_restart_accepts_once_then_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, _current = _fake_restart(monkeypatch, tmp_path)
    ticks = iter([0, 60])
    monkeypatch.setattr(debug_restart.time, "monotonic", lambda: next(ticks))
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == ["/api/login", "/api/restart"]
    assert "已接受一次" in capsys.readouterr().out


def test_interactive_restart_waits_for_new_listener_and_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, _current = _fake_restart(monkeypatch, tmp_path)
    new_client = FakeClient(base_url=client.base_url)
    new_client.instance = "new"
    new_client.get = lambda path: httpx.Response(
        200, json={"restart_instance": "new"} if path == "/api/status" else {}
    )
    monkeypatch.setattr(
        debug_restart.httpx,
        "Client",
        lambda **kwargs: client if kwargs["timeout"] == 5 else new_client,
    )
    monkeypatch.setattr(
        debug_restart,
        "_listener_identity",
        lambda port, owner: debug_restart.ListenerIdentity(42, "2026-01-01", 45, "2026-01-03"),
    )
    ticks = iter([0, 1, 2])
    monkeypatch.setattr(debug_restart.time, "monotonic", lambda: next(ticks, 2))
    monkeypatch.setattr(debug_restart.time, "sleep", lambda seconds: None)
    assert debug_restart._restart(tmp_path) == 0
    assert client.posts == ["/api/login", "/api/restart"]
    assert new_client.posts == []
    assert "新监听进程接管" in capsys.readouterr().out


def test_interactive_restart_refuses_unverified_listener_before_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        debug_restart, "running_web_instance", lambda home: ("http://127.0.0.1:29501", 42)
    )
    monkeypatch.setattr(debug_restart, "_listener_identity", lambda port, owner: None)
    monkeypatch.setattr(debug_restart, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(debug_restart.getpass, "getpass", lambda label: pytest.fail("不应读取密码"))
    assert debug_restart._restart(tmp_path) == 1


def test_interactive_restart_refuses_redirect_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _current = _fake_restart(monkeypatch, tmp_path)
    client.login_status = 302
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == ["/api/login"]


def test_interactive_restart_refuses_changed_listener_after_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _current = _fake_restart(monkeypatch, tmp_path)
    current = iter(
        [
            (
                "http://127.0.0.1:29501",
                debug_restart.ListenerIdentity(42, "2026-01-01", 44, "2026-01-02"),
            ),
            (
                "http://127.0.0.1:29501",
                debug_restart.ListenerIdentity(42, "2026-01-01", 44, "2026-01-02"),
            ),
            None,
        ]
    )
    monkeypatch.setattr(debug_restart, "_verified_target", lambda home: next(current, None))
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == ["/api/login"]


def test_two_level_launcher_chain_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        debug_restart, "running_web_instance", lambda home: ("http://127.0.0.1:29501", 42)
    )
    monkeypatch.setattr(debug_restart.sys, "platform", "win32")
    project = Path(debug_restart.__file__).resolve().parents[2]
    base_pythonw = tmp_path / "installed-python" / "pythonw.exe"
    monkeypatch.setattr(debug_restart, "_base_pythonw_path", lambda root: base_pythonw)
    processes = [
        {
            "ProcessId": 44,
            "ParentProcessId": 43,
            "ExecutablePath": str(base_pythonw),
            "Started": "2026-11-23T13:00:02Z",
        },
        {
            "ProcessId": 43,
            "ParentProcessId": 42,
            "ExecutablePath": str(project / ".venv" / "Scripts" / "pythonw.exe"),
            "CommandLine": "pythonw.exe -m aureus.cli.windows_launcher --tray --owner-pid 42",
            "Started": "2026-11-23T13:00:01Z",
        },
        {
            "ProcessId": 42,
            "ExecutablePath": str(project / "Aureus.exe"),
            "Started": "2026-11-23T13:00:00Z",
        },
    ]
    calls: list[list[str]] = []

    def fake_process(command: list[str], **_kwargs: Any) -> CompletedProcess[str]:
        calls.append(command)
        output = "44" if len(calls) % 2 else json.dumps(processes)
        return CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(debug_restart.subprocess, "run", fake_process)
    identity = debug_restart.ListenerIdentity(
        42, "2026-11-23T13:00:00Z", 44, "2026-11-23T13:00:02Z"
    )
    assert debug_restart._verified_target(tmp_path) == ("http://127.0.0.1:29501", identity)
    assert len(calls) == 2
    processes[0]["ExecutablePath"] = str(tmp_path / "wrong-install" / "pythonw.exe")
    assert debug_restart._verified_target(tmp_path) is None
    processes[0]["ExecutablePath"] = str(base_pythonw)
    processes[1]["ParentProcessId"] = 77
    assert debug_restart._verified_target(tmp_path) is None
    processes[1]["ParentProcessId"] = 42
    processes[1]["CommandLine"] = "pythonw.exe -m unrelated.module --tray --owner-pid 42"
    assert debug_restart._verified_target(tmp_path) is None


def test_base_pythonw_is_derived_only_from_the_project_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "checkout"
    venv = root / ".venv"
    venv.mkdir(parents=True)
    installation = tmp_path / "python-install"
    installation.mkdir()
    python = installation / "python.exe"
    python.write_bytes(b"synthetic executable")
    pythonw = installation / "pythonw.exe"
    pythonw.write_bytes(b"synthetic window executable")
    runtime = SimpleNamespace(
        prefix=str(venv),
        base_prefix=str(installation),
        _base_executable=str(python),
    )
    monkeypatch.setattr(debug_restart, "sys", runtime)
    assert debug_restart._base_pythonw_path(root) == pythonw
    pythonw.unlink()
    assert debug_restart._base_pythonw_path(root) is None
    runtime.prefix = str(tmp_path / "other-venv")
    assert debug_restart._base_pythonw_path(root) is None
    runtime.prefix = str(venv)
    runtime._base_executable = str(tmp_path / "missing-python.exe")
    assert debug_restart._base_pythonw_path(root) is None


def test_recovery_does_not_submit_password_or_claim_old_listener_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _current = _fake_restart(monkeypatch, tmp_path)
    monkeypatch.setattr(
        debug_restart,
        "_listener_identity",
        lambda port, owner: debug_restart.ListenerIdentity(42, "2026-01-01", 45, "2026-01-03"),
    )
    monkeypatch.setattr(debug_restart, "_old_listener_exited", lambda listener: False)
    monkeypatch.setattr(debug_restart.time, "sleep", lambda seconds: None)
    ticks = iter([0, 1, 60])
    monkeypatch.setattr(debug_restart.time, "monotonic", lambda: next(ticks))
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == ["/api/login", "/api/restart"]


def test_noninteractive_input_cannot_read_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _client, _current = _fake_restart(monkeypatch, tmp_path)
    monkeypatch.setattr(debug_restart, "_interactive_terminal", lambda: False)
    monkeypatch.setattr(debug_restart.getpass, "getpass", lambda label: pytest.fail("读取了密码"))
    assert debug_restart._restart(tmp_path) == 1


def test_old_listener_exit_requires_absence_not_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(debug_restart.sys, "platform", "win32")
    listener = debug_restart.ListenerIdentity(42, "2026-01-01", 44, "2026-01-02")
    output = ['[{"Started":"2026-01-02"}]']

    def fake_process(command: list[str], **_kwargs: Any) -> CompletedProcess[str]:
        assert "ConvertTo-Json -InputObject @(" in command[-1]
        return CompletedProcess(command, 0, stdout=output[0], stderr="")

    monkeypatch.setattr(debug_restart.subprocess, "run", fake_process)
    assert not debug_restart._old_listener_exited(listener)
    output[0] = "[]"
    assert debug_restart._old_listener_exited(listener)
    output[0] = '[{"Started":"2026-01-03"}]'
    assert debug_restart._old_listener_exited(listener)
    output[0] = ""
    assert not debug_restart._old_listener_exited(listener)


@pytest.mark.parametrize("interrupted", [EOFError, KeyboardInterrupt])
def test_confirmation_eof_or_interrupt_does_not_read_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interrupted: type[BaseException]
) -> None:
    client, _current = _fake_restart(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda label: (_ for _ in ()).throw(interrupted()))
    monkeypatch.setattr(debug_restart.getpass, "getpass", lambda label: pytest.fail("读取了密码"))
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == []


@pytest.mark.parametrize("interrupted", [EOFError, KeyboardInterrupt])
def test_password_eof_or_interrupt_never_sends_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interrupted: type[BaseException]
) -> None:
    client, _current = _fake_restart(monkeypatch, tmp_path)
    monkeypatch.setattr(
        debug_restart.getpass, "getpass", lambda label: (_ for _ in ()).throw(interrupted())
    )
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == []
