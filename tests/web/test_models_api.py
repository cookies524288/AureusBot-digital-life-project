"""模型配置接口：按条目保存、密码确认、错误不泄露本机路径（D84）。"""

import asyncio
import threading
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import pytest

from aureus.organs import ModelKind, OrganStrategy, ProviderProtocol, ReasoningLevel
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
        config["organs"]["replyer"]["models"] = ["missing"]
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
    assert response.json() == {"ok": True, "restart_required": True}
    text = rig.paths.models_config_file.read_text(encoding="utf-8")
    assert ORIGINAL.split("[[models]]")[0] in text
    assert ORIGINAL.split("[[models]]")[2] in text
    assert tomllib.loads(text)["models"][0]["identifier"] == "changed-id"
    assert rig.client.get("/api/overview").json()["restart_required"] is True


def test_unchanged_submission_does_not_write_or_set_restart_flag(rig: ConfigRig) -> None:
    config = saved_config(rig)
    before = rig.paths.models_config_file.stat().st_mtime_ns
    response = rig.client.put("/api/models", json={"config": config})
    assert response.json() == {"ok": True, "restart_required": False}
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
    empty = {"providers": [], "models": [], "organs": {}}
    assert rig.client.put("/api/models", json={"config": empty}).status_code == 200
    assert rig.client.get("/api/models").json()["config"] == {"config_version": 1, **empty}


def test_save_uses_the_current_file_and_an_unchanged_save_keeps_restart_required(
    rig: ConfigRig,
) -> None:
    config = saved_config(rig)
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
        assert response.json()["detail"] == "口令不对。"
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
