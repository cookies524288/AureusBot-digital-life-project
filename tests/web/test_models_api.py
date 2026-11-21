"""模型配置接口：按条目保存、密码确认、错误不泄露本机路径（D84）。"""

import asyncio
import threading
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import pytest

from aureus.common import ConfigError
from aureus.core.model_runtime import PreparedModels
from aureus.organs import ModelKind, ModelsConfig, OrganStrategy, ProviderProtocol, ReasoningLevel
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import GOOD

ORIGINAL = """# 模型配置注释
config_version = 1

[[providers]]
name = "cloud"  # 服务商的注释
protocol = "openai"
base_url = "https://api.example/v1"
api_key_env = "AUREUS_KEY_D84_TEST"

[[models]]
name = "first"  # 第一个模型
identifier = "first-id"
provider = "cloud"

[[models]]
name = "second"  # 第二个模型保持原样
identifier = "second-id"
provider = "cloud"

[organs.replyer]
models = ["first", "second"]  # 器官的注释
"""


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ConfigRig]:
    monkeypatch.delenv("AUREUS_KEY_D84_TEST", raising=False)
    made = ConfigRig(tmp_path)
    yield made
    made.close()


def saved_config(rig: ConfigRig) -> dict[str, Any]:
    rig.paths.models_config_file.write_bytes(ORIGINAL.encode("utf-8"))
    rig.log_in()
    return rig.client.get("/api/models").json()["config"]


@pytest.mark.parametrize("method", ["get", "put"])
def test_models_require_login_and_a_changed_password(tmp_path: Path, method: str) -> None:
    rig = ConfigRig(tmp_path, password_set=False)
    try:
        assert rig.client.request(method, "/api/models", json={}).status_code == 401
        rig.log_in("admin")
        assert rig.client.request(method, "/api/models", json={}).status_code == 403
    finally:
        rig.close()


def test_missing_models_file_returns_empty_config_and_literal_choices(rig: ConfigRig) -> None:
    rig.log_in()
    response = rig.client.get("/api/models")
    assert response.status_code == 200
    data = response.json()
    assert data["exists"] is False
    assert data["config"] == {"config_version": 1, "providers": [], "models": [], "organs": {}}
    assert data["choices"] == {
        "protocols": list(get_args(ProviderProtocol)),
        "kinds": list(get_args(ModelKind)),
        "strategies": list(get_args(OrganStrategy)),
        "reasoning_levels": list(get_args(ReasoningLevel)),
    }
    assert data["key_status"] == {}


def test_get_supplies_defaults_and_key_status_is_boolean(rig: ConfigRig) -> None:
    config = saved_config(rig)
    assert config["models"][0]["reasoning_levels"] == []
    assert config["organs"]["replyer"]["reasoning_effort"] is None
    assert rig.client.get("/api/models").json()["key_status"] == {"AUREUS_KEY_D84_TEST": False}


@pytest.mark.parametrize("models", [[], ["missing"]])
def test_empty_and_missing_organ_models_are_saved_with_warnings(
    rig: ConfigRig, models: list[str]
) -> None:
    config = saved_config(rig)
    config["organs"]["replyer"]["models"] = models
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 200
    assert response.json()["restart_required"] is False
    expected = "replyer未配置模型" if not models else "replyer引用了不存在的模型：missing"
    assert response.json()["warnings"] == [expected]
    assert rig.client.get("/api/models").json()["config"]["organs"]["replyer"]["models"] == models


def test_an_existing_builtin_organ_cannot_be_deleted(rig: ConfigRig) -> None:
    config = saved_config(rig)
    del config["organs"]["replyer"]
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 422
    assert "默认器官" in response.json()["detail"]
    assert rig.paths.models_config_file.read_bytes() == ORIGINAL.encode("utf-8")


@pytest.mark.parametrize("content", ["[[models]", "unexpected = 1", "config_version = 99"])
def test_broken_models_file_reports_a_chinese_string_without_local_path(
    rig: ConfigRig, content: str
) -> None:
    rig.paths.models_config_file.write_text(content, encoding="utf-8")
    rig.log_in()
    response = rig.client.get("/api/models")
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert "models.toml" in response.json()["detail"]
    assert str(rig.paths.home) not in response.text


@pytest.mark.parametrize("error", ["reference", "reasoning", "key_name"])
def test_invalid_models_do_not_change_file(rig: ConfigRig, error: str) -> None:
    config = saved_config(rig)
    if error == "reference":
        config["models"][0]["provider"] = "missing"
    elif error == "reasoning":
        config["organs"]["replyer"]["reasoning_effort"] = "high"
        config["models"][0]["reasoning_levels"] = ["high"]
    else:
        config["providers"][0]["api_key_env"] = "not-a-key-name"
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 422
    assert str(rig.paths.home) not in response.text
    assert rig.paths.models_config_file.read_bytes() == ORIGINAL.encode("utf-8")
    assert rig.service.restart_required is False


def test_edit_one_model_preserves_other_entries_and_comments(rig: ConfigRig) -> None:
    config = saved_config(rig)
    config["models"][0]["identifier"] = "changed-id"
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "restart_required": False, "warnings": []}
    text = rig.paths.models_config_file.read_text(encoding="utf-8")
    assert ORIGINAL.split("[[models]]")[0] in text
    assert ORIGINAL.split("[[models]]")[2] in text
    assert tomllib.loads(text)["models"][0]["identifier"] == "changed-id"
    assert rig.client.get("/api/overview").json()["restart_required"] is False
    assert rig.models.bus.is_registered("replyer")


def test_unchanged_submission_does_not_write_or_set_restart_flag(rig: ConfigRig) -> None:
    config = saved_config(rig)
    before = rig.paths.models_config_file.stat().st_mtime_ns
    response = rig.client.put("/api/models", json={"config": config})
    assert response.json() == {"ok": True, "restart_required": False, "warnings": []}
    assert rig.paths.models_config_file.stat().st_mtime_ns == before
    assert rig.paths.models_config_file.read_bytes() == ORIGINAL.encode("utf-8")


def test_missing_file_can_be_created_with_a_local_model(rig: ConfigRig) -> None:
    rig.log_in()
    config = {
        "providers": [{"name": "local", "protocol": "openai"}],
        "models": [{"name": "m", "identifier": "m", "provider": "local"}],
        "organs": {"replyer": {"models": ["m"]}},
    }
    assert rig.client.put("/api/models", json={"config": config}).status_code == 200
    saved = rig.client.get("/api/models").json()
    assert saved["exists"] is True
    assert saved["config"]["config_version"] == 1
    assert saved["config"]["organs"]["replyer"]["models"] == ["m"]


def test_reordering_keeps_each_unchanged_table_and_can_remove_all_entries(rig: ConfigRig) -> None:
    config = saved_config(rig)
    config["models"].reverse()
    assert rig.client.put("/api/models", json={"config": config}).status_code == 200
    text = rig.paths.models_config_file.read_text(encoding="utf-8")
    for entry in ORIGINAL.split("[[models]]")[1:]:
        assert entry.split("[organs.replyer]")[0] in text
    assert [m["name"] for m in tomllib.loads(text)["models"]] == ["second", "first"]
    empty = {"providers": [], "models": [], "organs": {"replyer": {"models": []}}}
    assert rig.client.put("/api/models", json={"config": empty}).status_code == 200
    saved = rig.client.get("/api/models").json()["config"]
    assert saved["providers"] == saved["models"] == []
    assert saved["organs"]["replyer"]["models"] == []


def test_save_uses_the_current_file_and_an_unchanged_save_keeps_restart_required(
    rig: ConfigRig,
) -> None:
    config = saved_config(rig)
    rig.service.restart_required = True  # 模型热重载不能清掉运行设置或密钥的重启提示。
    rig.paths.models_config_file.write_bytes(
        (ORIGINAL + "\n# 文件刚刚新增的注释\n").encode("utf-8")
    )
    config["models"][0]["identifier"] = "changed"
    assert rig.client.put("/api/models", json={"config": config}).status_code == 200
    assert "# 文件刚刚新增的注释" in rig.paths.models_config_file.read_text(encoding="utf-8")
    before = rig.paths.models_config_file.stat().st_mtime_ns
    assert rig.client.put("/api/models", json={"config": config}).json()["restart_required"] is True
    assert rig.paths.models_config_file.stat().st_mtime_ns == before


@pytest.mark.parametrize("body", [[], {"config": None}, {"config": []}])
def test_invalid_request_shape_returns_string_detail(rig: ConfigRig, body: Any) -> None:
    rig.log_in()
    response = rig.client.put("/api/models", json=body)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("change", ["base_url", "api_key_env", "new_provider"])
def test_sensitive_provider_changes_require_current_password(rig: ConfigRig, change: str) -> None:
    config = saved_config(rig)
    if change == "new_provider":
        config["providers"].append(
            {"name": "new", "protocol": "openai", "api_key_env": "AUREUS_KEY_NEW"}
        )
    else:
        config["providers"][0][change] = (
            "https://new.example/v1" if change == "base_url" else "AUREUS_KEY_NEW"
        )
    for password in (None, "wrong"):
        response = rig.client.put("/api/models", json={"config": config, "password": password})
        assert response.status_code == 403
        assert response.json()["detail"] == "密码不对。"
        assert rig.paths.models_config_file.read_bytes() == ORIGINAL.encode("utf-8")
    response = rig.client.put("/api/models", json={"config": config, "password": GOOD})
    assert response.status_code == 200


def test_wrong_confirmations_are_rate_limited(rig: ConfigRig) -> None:
    config = saved_config(rig)
    config["providers"][0]["base_url"] = "https://new.example"
    for _ in range(3):
        assert rig.client.put("/api/models", json={"config": config}).status_code == 403
    response = rig.client.put("/api/models", json={"config": config, "password": GOOD})
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_organs_and_local_providers_need_no_password_and_order_follows_submission(
    rig: ConfigRig,
) -> None:
    config = saved_config(rig)
    config["models"].reverse()
    config["models"].pop()
    config["organs"]["replyer"]["models"] = ["second"]
    config["providers"].append({"name": "local", "protocol": "openai"})
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 200
    raw = tomllib.loads(rig.paths.models_config_file.read_text(encoding="utf-8"))
    assert [m["name"] for m in raw["models"]] == ["second"]
    assert [p["name"] for p in raw["providers"]] == ["cloud", "local"]
    assert "# 第二个模型保持原样" in rig.paths.models_config_file.read_text(encoding="utf-8")


def test_replace_failure_preserves_file_and_restart_flag(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = saved_config(rig)
    config["models"][0]["identifier"] = "changed"

    def fail(*args: Any) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr("aureus.common.toml_edit.os.replace", fail)
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 500
    assert response.json()["detail"] == "写不进配置文件。"
    assert rig.paths.models_config_file.read_bytes() == ORIGINAL.encode("utf-8")
    assert list(rig.paths.config_dir.glob("*.tmp")) == []
    assert rig.service.restart_required is False


def test_files_run_in_a_worker_but_password_confirmation_runs_on_the_request_thread(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = saved_config(rig)
    config["providers"][0]["base_url"] = "https://new.example"
    worker_threads: list[int] = []
    confirm_threads: list[int] = []
    real_write = rig.service.write_models
    real_confirm = rig.auth.confirm_password

    def write(value: Any) -> None:
        worker_threads.append(threading.get_ident())
        real_write(value)

    def confirm(*args: Any) -> None:
        asyncio.get_running_loop()  # 线程池里没有这个事件循环。
        confirm_threads.append(threading.get_ident())
        real_confirm(*args)

    monkeypatch.setattr(rig.service, "write_models", write)
    monkeypatch.setattr(rig.auth, "confirm_password", confirm)
    assert (
        rig.client.put("/api/models", json={"config": config, "password": GOOD}).status_code == 200
    )
    assert worker_threads and confirm_threads
    assert worker_threads[0] != confirm_threads[0]


@pytest.mark.parametrize("failure", ["prepare", "replace"])
@pytest.mark.parametrize("restart_required", [False, True])
def test_failed_reload_preserves_the_file_active_organs_and_restart_flag(
    rig: ConfigRig,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    restart_required: bool,
) -> None:
    config = saved_config(rig)
    original = rig.models.prepare(ModelsConfig.model_validate(config))
    rig.models.apply(original)
    bus = rig.models.bus
    registered = dict(bus._organs)
    rig.service.restart_required = restart_required
    updates: list[frozenset[str]] = []
    rig.models.on_reload = updates.append
    writes: list[ModelsConfig] = []
    real_write = rig.service.write_models

    def write(value: ModelsConfig) -> None:
        writes.append(value)
        real_write(value)

    def fail_prepare(value: ModelsConfig) -> PreparedModels:
        raise ConfigError("新器官构造失败")

    def fail_replace(*args: Any) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr(rig.service, "write_models", write)
    if failure == "prepare":
        monkeypatch.setattr(rig.models, "prepare", fail_prepare)
    else:
        monkeypatch.setattr("aureus.common.toml_edit.os.replace", fail_replace)
    config["models"][0]["identifier"] = "changed-id"
    config["organs"]["custom"] = {"models": ["first"]}
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == (422 if failure == "prepare" else 500)
    expected = "新器官构造失败" if failure == "prepare" else "写不进配置文件。"
    assert response.json()["detail"] == expected
    assert len(writes) == (0 if failure == "prepare" else 1)
    assert rig.paths.models_config_file.read_bytes() == ORIGINAL.encode("utf-8")
    assert list(rig.paths.config_dir.glob("*.tmp")) == []
    assert rig.service.restart_required is restart_required
    assert rig.models.bus is bus and bus._organs == registered
    assert rig.models.available_organs == original.available
    assert updates == []


def test_successful_reload_prepares_then_writes_then_applies_on_the_event_loop(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = saved_config(rig)
    original = rig.models.prepare(ModelsConfig.model_validate(config))
    rig.models.apply(original)
    registered = dict(rig.models.bus._organs)
    calls: list[str] = []
    worker_threads: list[int] = []
    apply_threads: list[int] = []
    real_prepare, real_write, real_apply = (
        rig.models.prepare,
        rig.service.write_models,
        rig.models.apply,
    )

    def prepare(value: ModelsConfig) -> PreparedModels:
        worker_threads.append(threading.get_ident())
        assert rig.paths.models_config_file.read_bytes() == ORIGINAL.encode("utf-8")
        assert rig.models.bus._organs == registered
        prepared = real_prepare(value)
        calls.append("prepared")
        return prepared

    def write(value: ModelsConfig) -> None:
        worker_threads.append(threading.get_ident())
        assert calls == ["prepared"]
        assert rig.models.bus._organs == registered
        real_write(value)
        assert rig.models.bus._organs == registered
        calls.append("written")

    def apply(value: PreparedModels) -> None:
        asyncio.get_running_loop()
        apply_threads.append(threading.get_ident())
        assert calls == ["prepared", "written"]
        saved = tomllib.loads(rig.paths.models_config_file.read_text(encoding="utf-8"))
        assert saved["models"][0]["identifier"] == "changed-id"
        real_apply(value)
        calls.append("applied")

    monkeypatch.setattr(rig.models, "prepare", prepare)
    monkeypatch.setattr(rig.service, "write_models", write)
    monkeypatch.setattr(rig.models, "apply", apply)
    config["models"][0]["identifier"] = "changed-id"
    config["organs"]["custom"] = {"models": ["first"]}
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 200
    assert calls == ["prepared", "written", "applied"]
    assert len(worker_threads) == 2 and len(apply_threads) == 1
    assert all(thread != apply_threads[0] for thread in worker_threads)
    assert rig.models.available_organs == frozenset({"replyer", "custom"})
    assert rig.models.bus._organs["replyer"] is not original.organs[0]


def test_an_unchanged_save_does_not_prepare_or_replace_active_organs(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = saved_config(rig)
    original = rig.models.prepare(ModelsConfig.model_validate(config))
    rig.models.apply(original)
    registered = dict(rig.models.bus._organs)

    def reject_call(*args: Any) -> None:
        raise AssertionError("未改动的配置不应重建或替换器官")

    monkeypatch.setattr(rig.models, "prepare", reject_call)
    monkeypatch.setattr(rig.models, "apply", reject_call)
    monkeypatch.setattr(rig.service, "write_models", reject_call)
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 200
    assert rig.models.bus._organs == registered
    assert rig.models.available_organs == original.available


@pytest.mark.parametrize("name", ["planner", "judge", "summarizer"])
def test_other_existing_builtin_organs_cannot_be_removed(rig: ConfigRig, name: str) -> None:
    config = saved_config(rig)
    config["organs"][name] = {"models": []}
    original = ModelsConfig.model_validate(config)
    rig.service.write_models(original)
    rig.models.apply(rig.models.prepare(original))
    before = rig.paths.models_config_file.read_bytes()
    registered = dict(rig.models.bus._organs)
    del config["organs"][name]
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 422
    assert "默认器官不可删除" in response.json()["detail"]
    assert rig.paths.models_config_file.read_bytes() == before
    assert rig.models.bus._organs == registered


def test_deleting_a_custom_organ_removes_it_from_the_running_bus(rig: ConfigRig) -> None:
    config = saved_config(rig)
    config["organs"]["custom"] = {"models": ["first"]}
    original = ModelsConfig.model_validate(config)
    rig.service.write_models(original)
    rig.models.apply(rig.models.prepare(original))
    assert rig.models.bus.is_registered("custom")
    del config["organs"]["custom"]
    response = rig.client.put("/api/models", json={"config": config})
    assert response.status_code == 200
    assert rig.models.bus.list_organs() == ["replyer"]
    assert rig.models.available_organs == frozenset({"replyer"})
    assert (
        "custom"
        not in tomllib.loads(rig.paths.models_config_file.read_text(encoding="utf-8"))["organs"]
    )
