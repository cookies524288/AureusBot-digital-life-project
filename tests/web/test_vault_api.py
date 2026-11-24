"""通用密钥的元数据接口、稳定引用与运行中内存刷新。"""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from aureus.common import configure_logging, shutdown_logging
from aureus.common.logging_setup import LOG_FILE_NAME, REDACTED
from aureus.common.vault import SecretVault
from aureus.organs import KeyResolver, LLMOrgan, ModelsConfig
from aureus.organs.interface import OrganCallError
from tests.organs.test_llm_adapter import FakeCompletion, ask, make_response
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import GOOD

VALUE = "fake-vault-secret-for-d87-tests"
NEW_VALUE = "fake-updated-vault-secret"


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[ConfigRig]:
    made = ConfigRig(tmp_path)
    made.log_in()
    yield made
    made.close()
    shutdown_logging()


def create(rig: ConfigRig, name: str = "模型密钥", kind: str = "api_key") -> str:
    response = rig.client.post(
        "/api/keys", json={"name": name, "kind": kind, "value": VALUE, "password": GOOD}
    )
    assert response.status_code == 200, response.text
    assert VALUE not in response.text
    assert response.json()["restart_required"] is False
    return response.json()["id"]


@pytest.mark.parametrize("kind", ["api_key", "password", "other"])
def test_metadata_only_and_no_detail_or_value_endpoint(rig: ConfigRig, kind: str) -> None:
    entry_id = create(rig, kind=kind)
    listed = rig.client.get("/api/keys")
    assert listed.json() == {
        "vault": "ok",
        "names": ["模型密钥"],
        "entries": [{"id": entry_id, "name": "模型密钥", "kind": kind}],
    }
    assert VALUE not in listed.text + rig.client.get("/api/models").text
    assert rig.client.get("/api/keys/" + entry_id).status_code == 404
    assert rig.client.get("/api/keys/" + entry_id + "/value").status_code == 404
    assert VALUE.encode() not in rig.paths.vault_file.read_bytes()


def test_rename_retains_model_reference_and_delete_immediately_removes_it(rig: ConfigRig) -> None:
    entry_id = create(rig)
    config = rig.client.get("/api/models").json()["config"]
    config["providers"] = [{"name": "测试厂商", "protocol": "openai", "api_key_ref": entry_id}]
    assert rig.client.put("/api/models", json={"config": config}).status_code == 403
    assert (
        rig.client.put(
            "/api/models", json={"config": config}, headers={"Origin": str(rig.client.base_url)}
        ).status_code
        == 200
    )
    before = rig.paths.models_config_file.read_bytes()
    renamed = rig.client.post(
        "/api/keys/rename", json={"id": entry_id, "name": "新名称", "password": GOOD}
    )
    assert renamed.status_code == 200
    assert rig.paths.models_config_file.read_bytes() == before
    secret = rig.models.key_resolver.get_vault(entry_id)
    assert secret is not None and secret.get_secret_value() == VALUE
    assert rig.client.get("/api/models").json()["key_status"] == {entry_id: True}
    assert rig.client.get("/api/keys").json()["names"] == ["新名称"]
    assert (
        rig.client.post("/api/keys/remove", json={"id": entry_id, "password": GOOD}).status_code
        == 200
    )
    assert rig.models.key_resolver.get_vault(entry_id) is None
    assert rig.client.get("/api/models").json()["key_status"] == {entry_id: False}
    assert rig.paths.models_config_file.read_bytes() == before


def test_legacy_config_survives_rename_and_cli_upsert(rig: ConfigRig) -> None:
    name = "AUREUS_KEY_D87_LEGACY"
    assert (
        rig.client.put(
            "/api/keys", json={"name": name, "value": VALUE, "password": GOOD}
        ).status_code
        == 200
    )
    entry_id = rig.client.get("/api/keys").json()["entries"][0]["id"]
    assert (
        rig.client.post(
            "/api/keys/rename", json={"id": entry_id, "name": "中文名", "password": GOOD}
        ).status_code
        == 200
    )
    secret = rig.models.key_resolver.get(name)
    assert secret is not None and secret.get_secret_value() == VALUE
    vault = SecretVault(rig.paths.vault_file, rig.paths.master_key_file)
    vault.set(name, "fake-cli-updated")
    assert len(vault.load_entries()) == 1
    assert vault.load_entries()[0].name == "中文名"
    assert vault.load_entries()[0].id == entry_id


@pytest.mark.parametrize("kind", ["api_key", "password", "other"])
def test_update_keeps_entry_metadata_and_never_returns_values(rig: ConfigRig, kind: str) -> None:
    entry_id = create(rig, name="AUREUS_KEY_UPDATE", kind=kind)
    before = rig.client.get("/api/keys").json()
    value = f"  {NEW_VALUE}\n  "
    response = rig.client.post(
        "/api/keys/update", json={"id": entry_id, "value": value, "password": GOOD}
    )
    assert response.json() == {"ok": True, "restart_required": False}
    listed = rig.client.get("/api/keys")
    assert listed.json() == before
    assert all(secret not in response.text + listed.text for secret in (VALUE, NEW_VALUE))
    vault = SecretVault(rig.paths.vault_file, rig.paths.master_key_file)
    entry = vault.load_entries()[0]
    assert entry.legacy_name == "AUREUS_KEY_UPDATE"
    assert entry.value.get_secret_value() == (value.strip() if kind == "api_key" else value)


def test_update_requires_password_and_existing_id(rig: ConfigRig) -> None:
    entry_id = create(rig)
    before = rig.paths.vault_file.read_bytes()
    response = rig.client.post("/api/keys/update", json={"id": entry_id, "value": NEW_VALUE})
    assert response.status_code == 403
    response = rig.client.post(
        "/api/keys/update", json={"id": "0" * 32, "value": NEW_VALUE, "password": GOOD}
    )
    assert response.status_code == 404
    response = rig.client.post(
        "/api/keys/update", json={"id": entry_id, "value": "   ", "password": GOOD}
    )
    assert response.status_code == 422
    assert rig.paths.vault_file.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "x" * 129},
        {"name": "x\ny"},
        {"name": "\n名称"},
        {"name": []},
        {"kind": "invalid"},
        {"kind": []},
        {"value": ""},
        {"value": " "},
        {"value": []},
        {"value": "x" * 4097},
    ],
)
def test_invalid_create_never_echoes_input_or_creates_files(
    rig: ConfigRig, payload: dict[str, Any]
) -> None:
    body = {"name": "测试名称", "kind": "api_key", "value": VALUE, "password": GOOD, **payload}
    response = rig.client.post("/api/keys", json=body)
    assert response.status_code == 422
    assert VALUE not in response.text
    assert not rig.paths.vault_file.exists()


@pytest.mark.parametrize("action", ["remove", "update"])
def test_duplicate_names_and_failed_file_writes_leave_memory_unchanged(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    entry_id = create(rig)
    before = rig.paths.vault_file.read_bytes()
    assert (
        rig.client.post(
            "/api/keys",
            json={"name": "模型密钥", "kind": "other", "value": "fake-new", "password": GOOD},
        ).status_code
        == 422
    )
    assert rig.paths.vault_file.read_bytes() == before
    import aureus.common.vault as module

    def fail(*args: Any) -> None:
        raise OSError(VALUE)

    monkeypatch.setattr(module, "_write_atomically", fail)
    response = rig.client.post(
        f"/api/keys/{action}", json={"id": entry_id, "value": NEW_VALUE, "password": GOOD}
    )
    assert response.status_code == 500 and VALUE not in response.text
    assert rig.paths.vault_file.read_bytes() == before
    secret = rig.models.key_resolver.get_vault(entry_id)
    assert secret is not None and secret.get_secret_value() == VALUE


def test_new_secret_mutations_run_in_workers(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = asyncio.to_thread

    async def observe(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(func.__name__)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", observe)
    entry_id = create(rig)
    assert (
        rig.client.post(
            "/api/keys/rename", json={"id": entry_id, "name": "新名字", "password": GOOD}
        ).status_code
        == 200
    )
    assert (
        rig.client.post("/api/keys/remove", json={"id": entry_id, "password": GOOD}).status_code
        == 200
    )
    assert all(name in calls for name in ("create_key", "rename_key", "remove_key"))


def test_all_types_and_removed_values_remain_hidden_in_logs(rig: ConfigRig, tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    configure_logging(logs, "INFO", to_console=False)
    entry_id = create(rig, kind="password")
    assert (
        rig.client.post("/api/keys/remove", json={"id": entry_id, "password": GOOD}).status_code
        == 200
    )
    logger.info("请求结束：{}", VALUE)
    try:
        raise ValueError(VALUE)
    except ValueError:
        logger.exception("模拟在途请求失败")
    shutdown_logging()
    text = (logs / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert VALUE not in text and REDACTED in text


def test_runtime_call_uses_memory_and_refuses_deleted_or_non_api_keys(rig: ConfigRig) -> None:
    entry_id = create(rig)
    password_id = create(rig, name="网站密码", kind="password")
    config = ModelsConfig.model_validate(
        {
            "providers": [{"name": "p", "protocol": "openai", "api_key_ref": entry_id}],
            "models": [{"name": "m", "identifier": "fake-model", "provider": "p"}],
            "organs": {"replyer": {"models": ["m"]}},
        }
    )
    completion = FakeCompletion(make_response("测试成功"), make_response("换值成功"))
    organ = LLMOrgan.from_config("replyer", config, rig.models.key_resolver, completion)
    asyncio.run(organ.process(ask()))
    assert completion.calls[0]["api_key"] == VALUE
    assert (
        rig.client.post(
            "/api/keys/update", json={"id": entry_id, "value": NEW_VALUE, "password": GOOD}
        ).status_code
        == 200
    )
    asyncio.run(organ.process(ask()))
    assert completion.calls[1]["api_key"] == NEW_VALUE
    assert rig.models.key_resolver.get_vault(password_id) is None
    assert (
        rig.client.post("/api/keys/remove", json={"id": entry_id, "password": GOOD}).status_code
        == 200
    )
    with pytest.raises(OrganCallError, match="API key"):
        asyncio.run(organ.process(ask()))
    assert len(completion.calls) == 2
    # 重启使用同一主密钥自动解锁，不依赖网页登录原文。
    restarted = KeyResolver(
        rig.paths.env_file,
        environ={"PATH": VALUE},
        vault=SecretVault(rig.paths.vault_file, rig.paths.master_key_file),
    )
    assert restarted.get("PATH") is None
    assert restarted.get_vault(password_id) is None
    assert VALUE in restarted.all_secret_values()
