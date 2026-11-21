"""密钥接口：只接受本机确认、从不回显原文、写入交给线程（D84）。"""

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aureus.common import ConfigError
from aureus.common.vault import SecretVault
from aureus.web.config_api import LOCAL_KEYS_ONLY, MAX_KEY_CHARS
from tests.web.config_rig import ConfigRig
from tests.web.test_models_api import ORIGINAL
from tests.web.test_timeline_api import GOOD

NAME = "AUREUS_KEY_D84_TEST"
VALUE = "sk-fake-d84-secret-only-for-tests"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ConfigRig]:
    monkeypatch.delenv(NAME, raising=False)
    made = ConfigRig(tmp_path)
    made.log_in()
    yield made
    made.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/keys"),
        ("put", "/api/keys"),
        ("post", "/api/keys"),
        ("post", "/api/keys/rename"),
        ("post", "/api/keys/remove"),
    ],
)
def test_keys_require_login_and_a_changed_password(tmp_path: Path, method: str, path: str) -> None:
    rig = ConfigRig(tmp_path, password_set=False)
    try:
        assert rig.client.request(method, path, json={}).status_code == 401
        rig.log_in("admin")
        assert rig.client.request(method, path, json={}).status_code == 403
    finally:
        rig.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", "/api/keys"),
        ("post", "/api/keys"),
        ("post", "/api/keys/rename"),
        ("post", "/api/keys/remove"),
    ],
)
def test_remote_sources_are_rejected_before_password_checks(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    remote = TestClient(rig.app, client=("192.168.1.20", 50000))
    remote.cookies.update(rig.client.cookies)
    confirmed: list[bool] = []
    original = rig.auth.confirm_password

    def confirm(*args: Any) -> None:
        confirmed.append(True)
        original(*args)

    monkeypatch.setattr(rig.auth, "confirm_password", confirm)
    for _ in range(10):
        response = remote.request(
            method, path, json={"name": NAME, "value": VALUE, "password": "wrong"}
        )
        assert response.status_code == 403
        assert response.json()["detail"] == LOCAL_KEYS_ONLY
    assert confirmed == []
    response = rig.client.put("/api/keys", json={"name": NAME, "value": VALUE, "password": GOOD})
    assert response.status_code == 200
    assert confirmed == [True]


def test_keys_need_the_current_password_and_count_failures(rig: ConfigRig) -> None:
    for _ in range(3):
        response = rig.client.put(
            "/api/keys", json={"name": NAME, "value": VALUE, "password": "wrong"}
        )
        assert response.status_code == 403
        assert VALUE not in response.text
    assert (
        rig.client.put(
            "/api/keys", json={"name": NAME, "value": VALUE, "password": GOOD}
        ).status_code
        == 429
    )
    assert not rig.paths.vault_file.exists()


@pytest.mark.parametrize("value", ["", "  \n  ", "x" * (MAX_KEY_CHARS + 1), None, 1, [VALUE]])
def test_invalid_values_return_chinese_detail_without_echoing_input(
    rig: ConfigRig, caplog: pytest.LogCaptureFixture, value: Any
) -> None:
    caplog.set_level(logging.INFO)
    response = rig.client.put("/api/keys", json={"name": NAME, "value": value, "password": GOOD})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert VALUE not in response.text + caplog.text
    if isinstance(value, str) and value.strip():
        assert value not in response.text + caplog.text
    assert not rig.paths.vault_file.exists()


@pytest.mark.parametrize("name", ["invalid", "AUREUS_KEY_lowercase", "", None, [VALUE]])
def test_invalid_names_do_not_echo_the_submitted_value(rig: ConfigRig, name: Any) -> None:
    response = rig.client.put("/api/keys", json={"name": name, "value": VALUE, "password": GOOD})
    assert response.status_code == 422
    assert VALUE not in response.text


def test_first_write_creates_vault_lists_only_names_and_updates_model_status_immediately(
    rig: ConfigRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    rig.paths.models_config_file.write_bytes(ORIGINAL.encode("utf-8"))
    assert rig.client.get("/api/keys").json() == {"vault": "none", "names": [], "entries": []}
    assert rig.client.get("/api/models").json()["key_status"] == {NAME: False}
    response = rig.client.put(
        "/api/keys", json={"name": NAME, "value": f" {VALUE} \n", "password": GOOD}
    )
    assert response.json() == {"ok": True, "restart_required": False}
    assert rig.paths.master_key_file.exists()
    assert rig.paths.vault_file.exists()
    assert SecretVault(rig.paths.vault_file, rig.paths.master_key_file).load() == {NAME: VALUE}
    listed = rig.client.get("/api/keys")
    entries = SecretVault(rig.paths.vault_file, rig.paths.master_key_file).load_entries()
    assert listed.json() == {
        "vault": "ok",
        "names": [NAME],
        "entries": [{"id": entries[0].id, "name": NAME, "kind": "api_key"}],
    }
    models = rig.client.get("/api/models")
    assert models.json()["key_status"] == {NAME: True}
    assert VALUE not in response.text + listed.text + models.text + caplog.text


@pytest.mark.parametrize("broken", ["missing_key", "invalid_key", "tampered", "future"])
def test_unreadable_vault_is_reported_and_never_recreated(
    rig: ConfigRig, broken: str, caplog: pytest.LogCaptureFixture
) -> None:
    vault = SecretVault(rig.paths.vault_file, rig.paths.master_key_file)
    vault.set(NAME, VALUE)
    if broken == "missing_key":
        rig.paths.master_key_file.unlink()  # 临时测试文件。
    elif broken == "invalid_key":
        rig.paths.master_key_file.write_text("{}", encoding="utf-8")
    elif broken == "tampered":
        rig.paths.vault_file.write_text('{"format_version":1,"token":"tampered"}', encoding="utf-8")
    else:
        raw = json.loads(rig.paths.vault_file.read_text(encoding="utf-8"))
        raw["format_version"] = 999
        rig.paths.vault_file.write_text(json.dumps(raw), encoding="utf-8")
    before = rig.paths.vault_file.read_bytes()
    assert rig.client.get("/api/keys").json() == {"vault": "unreadable", "names": [], "entries": []}
    for method, path in (("put", "/api/keys"), ("post", "/api/keys/remove")):
        response = rig.client.request(
            method, path, json={"name": NAME, "value": VALUE, "password": GOOD}
        )
        assert response.status_code == 422
        assert VALUE not in response.text + caplog.text
        assert str(rig.paths.home) not in response.text
        assert rig.paths.vault_file.read_bytes() == before
    assert rig.service.restart_required is False


def test_remove_missing_key_returns_404_without_setting_restart_required(rig: ConfigRig) -> None:
    response = rig.client.post("/api/keys/remove", json={"name": NAME, "password": GOOD})
    assert response.status_code == 404
    assert response.json()["detail"] == "没有这个密钥。"
    assert rig.service.restart_required is False


def test_remove_key_only_returns_success_and_names_disappear(rig: ConfigRig) -> None:
    SecretVault(rig.paths.vault_file, rig.paths.master_key_file).set(NAME, VALUE)
    response = rig.client.post("/api/keys/remove", json={"name": NAME, "password": GOOD})
    assert response.json() == {"ok": True, "restart_required": False}
    assert rig.client.get("/api/keys").json() == {"vault": "ok", "names": [], "entries": []}
    assert VALUE not in response.text


def test_key_status_reads_current_files_and_never_falls_back_from_a_broken_vault(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.paths.env_file.write_text(f"{NAME}={VALUE}\n", encoding="utf-8")
    assert rig.service.key_status([NAME]) == {NAME: True}
    rig.paths.env_file.write_text(f"{NAME}=  \n", encoding="utf-8")
    assert rig.service.key_status([NAME]) == {NAME: False}
    monkeypatch.setenv(NAME, VALUE)
    assert rig.service.key_status([NAME]) == {NAME: True}
    rig.paths.vault_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        rig.service.key_status([NAME])


def test_key_write_uses_to_thread_and_has_no_running_loop_in_the_worker(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = asyncio.to_thread
    write_key = rig.service.write_key

    async def observe(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(func.__name__)
        return await original(func, *args, **kwargs)

    def write(name: str, value: str) -> None:
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        write_key(name, value)

    monkeypatch.setattr(asyncio, "to_thread", observe)
    monkeypatch.setattr(rig.service, "write_key", write)
    assert (
        rig.client.put(
            "/api/keys", json={"name": NAME, "value": VALUE, "password": GOOD}
        ).status_code
        == 200
    )
    assert "write" in calls


@pytest.mark.parametrize("content", ["[", '"secret"', '["secret"]'])
def test_invalid_json_and_body_shapes_do_not_echo_input(rig: ConfigRig, content: str) -> None:
    response = rig.client.put(
        "/api/keys", content=content, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert "secret" not in response.text
