"""调试重启入口只打开已发布地址，不启动实例或发重启请求。"""

from pathlib import Path
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
) -> tuple[FakeClient, list[tuple[str, int] | None]]:
    target = ("http://127.0.0.1:29501", 42)
    current: list[tuple[str, int] | None] = [target]
    client = FakeClient(base_url=target[0])
    monkeypatch.setattr(debug_restart, "_verified_target", lambda home: current[0])
    monkeypatch.setattr(debug_restart, "_listener_owner", lambda port: (43, 42))
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
        current[0] = ("http://127.0.0.1:29502", 77)
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
    owners = iter([(43, 42), (43, 42), (44, 42)])
    monkeypatch.setattr(debug_restart, "_listener_owner", lambda port: next(owners, (44, 42)))
    ticks = iter([0, 1, 2])
    monkeypatch.setattr(debug_restart.time, "monotonic", lambda: next(ticks, 2))
    monkeypatch.setattr(debug_restart.time, "sleep", lambda seconds: None)
    assert debug_restart._restart(tmp_path) == 0
    assert client.posts == ["/api/login", "/api/restart"]
    assert new_client.posts == ["/api/login"]
    assert "实例身份已变化" in capsys.readouterr().out


def test_interactive_restart_refuses_unverified_listener_before_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        debug_restart, "running_web_instance", lambda home: ("http://127.0.0.1:29501", 42)
    )
    monkeypatch.setattr(debug_restart, "_listener_owner", lambda port: (77, 88))
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
    owners = iter([(43, 42), (44, 42)])
    monkeypatch.setattr(debug_restart, "_listener_owner", lambda port: next(owners, (44, 42)))
    assert debug_restart._restart(tmp_path) == 1
    assert client.posts == ["/api/login"]
