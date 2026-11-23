"""通过真实网页配置接口和启动加载器验收逐项恢复（D91）。"""

import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, RuntimeConfig, load_runtime_config, shutdown_logging
from aureus.core.runtime import open_core
from aureus.organs import ModelsConfig, load_models_config
from aureus.organs.model_config import ProviderConfig
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import GOOD
from tests.web.test_web_acceptance_flow import IDENTITY

RUNTIME_RESTORE = "/api/config/restore-default"
MODEL_RESTORE = "/api/models/restore-default"


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[ConfigRig]:
    made = ConfigRig(tmp_path)
    made.log_in()
    try:
        yield made
    finally:
        made.close()


def test_runtime_restore_removes_only_that_override_and_restart_reads_new_defaults(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = rig.paths.runtime_config_file
    rig.paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    identity = rig.paths.identity_file.read_bytes()
    assert rig.client.get("/api/config").json()["exists"] is False
    assert rig.client.post(RUNTIME_RESTORE, json={"field": "log_level"}).json()["changed"] is False
    assert not path.exists()
    # 与旧默认碰巧相同的显式值也要保留。
    path.write_text("[heartbeat]\nsaving_message_limit = 10 # 显式覆盖\n", encoding="utf-8")
    response = rig.client.put(
        "/api/config",
        json={
            "changes": {
                "heartbeat": {
                    "tick_interval_seconds": 13,
                    "recent_message_limit": 27,
                }
            }
        },
    )
    assert response.status_code == 200
    field = "heartbeat.tick_interval_seconds"
    assert rig.client.post(RUNTIME_RESTORE, json={"field": field}).json() == {
        "value": HeartbeatConfig().tick_interval_seconds,
        "changed": True,
        "restart_required": True,
    }
    raw = path.read_text(encoding="utf-8")
    assert "# 显式覆盖" in raw
    assert tomllib.loads(raw)["heartbeat"] == {
        "saving_message_limit": 10,
        "recent_message_limit": 27,
    }
    assert rig.paths.identity_file.read_bytes() == identity
    assert load_runtime_config(rig.paths).heartbeat.recent_message_limit == 27

    fields = HeartbeatConfig.model_fields
    for name, default in (
        ("tick_interval_seconds", 99.0),
        ("low_message_limit", 6),
        ("saving_message_limit", 11),
    ):
        monkeypatch.setattr(fields[name], "default", default)
    try:
        HeartbeatConfig.model_rebuild(force=True)
        RuntimeConfig.model_rebuild(force=True)
        assert rig.client.post(RUNTIME_RESTORE, json={"field": field}).json()["value"] == 99.0
        assert path.read_text(encoding="utf-8") == raw
        with open_core(rig.paths, fake_organs=True, clock=rig.clock, to_console=False) as core:
            heartbeat = core.settings.runtime.heartbeat
            assert heartbeat.tick_interval_seconds == 99.0
            assert heartbeat.low_message_limit == 6
            assert heartbeat.saving_message_limit == 10
            assert heartbeat.recent_message_limit == 27
    finally:
        monkeypatch.undo()
        HeartbeatConfig.model_rebuild(force=True)
        RuntimeConfig.model_rebuild(force=True)
        shutdown_logging()


def test_inbox_read_limit_saves_and_restores_for_restart(rig: ConfigRig) -> None:
    client = rig.client
    path = rig.paths.runtime_config_file
    assert client.get("/api/config").json()["config"]["heartbeat"]["inbox_read_limit"] == 35

    saved = client.put(
        "/api/config",
        json={"changes": {"heartbeat": {"inbox_read_limit": 72, "recent_message_limit": 27}}},
    )
    assert saved.status_code == 200
    assert saved.json()["restart_required"] is True
    assert client.get("/api/config").json()["config"]["heartbeat"]["inbox_read_limit"] == 72
    assert load_runtime_config(rig.paths).heartbeat.inbox_read_limit == 72

    before = path.read_bytes()
    for invalid in (0, 201, 35.5):
        rejected = client.put(
            "/api/config", json={"changes": {"heartbeat": {"inbox_read_limit": invalid}}}
        )
        assert rejected.status_code == 422
    assert path.read_bytes() == before

    restored = client.post(RUNTIME_RESTORE, json={"field": "heartbeat.inbox_read_limit"})
    assert restored.status_code == 200
    assert restored.json() == {"value": 35, "changed": True, "restart_required": True}
    assert tomllib.loads(path.read_text(encoding="utf-8"))["heartbeat"] == {
        "recent_message_limit": 27,
    }
    assert client.get("/api/config").json()["config"]["heartbeat"]["inbox_read_limit"] == 35
    assert load_runtime_config(rig.paths).heartbeat.inbox_read_limit == 35


@pytest.mark.parametrize(
    "field",
    [
        "memory.enabled",
        "memory.recall_enabled",
        "memory.heuristic_recall_enabled",
        "memory.max_results",
        "memory.index_batch",
        "memory.consolidation_enabled",
        "memory.consolidation_extract_enabled",
        "memory.consolidation_batch",
        "memory.consolidation_half_life_days",
        "language.recall_enabled",
        "language.learning_enabled",
        "language.organ",
        "language.reference_chars",
    ],
)
def test_language_and_memory_restore_preserves_other_overrides(rig: ConfigRig, field: str) -> None:
    changes = {
        "memory": {
            "enabled": False,
            "recall_enabled": False,
            "heuristic_recall_enabled": True,
            "max_results": 12,
            "index_batch": 321,
            "consolidation_enabled": True,
            "consolidation_extract_enabled": True,
            "consolidation_batch": 32,
            "consolidation_half_life_days": 15.0,
        },
        "language": {
            "recall_enabled": False,
            "learning_enabled": True,
            "organ": "planner",
            "reference_chars": 1500,
        },
    }
    client = rig.client
    assert client.put("/api/config", json={"changes": changes}).status_code == 200
    path = rig.paths.runtime_config_file
    assert tomllib.loads(path.read_text(encoding="utf-8")) == changes
    section, name = field.split(".")
    default = RuntimeConfig().model_dump()[section][name]
    assert changes[section].pop(name) != default
    restored = client.post(RUNTIME_RESTORE, json={"field": field})
    assert restored.status_code == 200
    assert restored.json() == {"value": default, "changed": True, "restart_required": True}
    assert tomllib.loads(path.read_text(encoding="utf-8")) == changes
    changes[section][name] = default
    page_config = client.get("/api/config").json()["config"]
    loaded = load_runtime_config(rig.paths).model_dump()
    for group, values in changes.items():
        assert page_config[group] == loaded[group] == values


def test_model_restore_reloads_and_sparse_saves_follow_changed_defaults(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = rig.paths.models_config_file
    path.write_text(
        '[[providers]]\nname = "local"\nprotocol = "openai"\ntimeout_seconds = 21\n'
        '[[models]]\nname = "m"\nidentifier = "m"\nprovider = "local"\n'
        'tool_calls = true # 旧配置\n[organs.replyer]\nmodels = ["m"]\n',
        encoding="utf-8",
    )
    config = rig.client.get("/api/models").json()["config"]
    config["providers"][0]["max_retry"] = 7
    config["models"][0]["temperature"] = 0.4
    config["models"][0]["name"] = "renamed"
    config["organs"]["replyer"]["models"] = ["renamed"]
    config["organs"]["replyer"]["max_tokens"] = 200
    assert (
        rig.client.put(
            "/api/models", json={"config": config, "rename": ["models", "m", "renamed"]}
        ).status_code
        == 200
    )
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert set(raw["models"][0]) == {"name", "identifier", "provider", "tool_calls", "temperature"}
    assert "strategy" not in raw["organs"]["replyer"]
    assert "# 旧配置" in path.read_text(encoding="utf-8")

    cases = [
        (
            "providers",
            "local",
            "timeout_seconds",
            ProviderConfig.model_fields["timeout_seconds"].default,
        ),
        ("models", "renamed", "temperature", None),
        ("organs", "replyer", "max_tokens", None),
    ]
    applied = []
    rig.models.on_reload = applied.append
    for section, name, field, default in cases:
        params = {"section": section, "name": name, "field": field}
        assert rig.client.post(MODEL_RESTORE, json=params).json()["value"] == default
        assert rig.client.post(MODEL_RESTORE, json=params).json()["value"] == default
    assert len(applied) == len(cases)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["providers"][0] == {"name": "local", "protocol": "openai", "max_retry": 7}
    assert raw["models"][0]["tool_calls"] is True
    assert "temperature" not in raw["models"][0]
    assert raw["organs"]["replyer"] == {"models": ["renamed"]}

    fields = ProviderConfig.model_fields
    monkeypatch.setattr(fields["timeout_seconds"], "default", 44.0)
    monkeypatch.setattr(fields["max_retry"], "default", 3)
    try:
        ProviderConfig.model_rebuild(force=True)
        ModelsConfig.model_rebuild(force=True)
        loaded = load_models_config(rig.paths)
        assert loaded.providers[0].timeout_seconds == 44.0
        assert loaded.providers[0].max_retry == 7
        assert loaded.routes_for("replyer")[0].max_tokens is None
    finally:
        monkeypatch.undo()
        ProviderConfig.model_rebuild(force=True)
        ModelsConfig.model_rebuild(force=True)


def test_restore_permissions_unknown_keys_and_invalid_result_keep_files(rig: ConfigRig) -> None:
    runtime = rig.paths.runtime_config_file
    runtime.write_text("[web]\nport = 30000\nsession_days = 12\n", encoding="utf-8")
    models = rig.paths.models_config_file
    models.write_text(
        '[[providers]]\nname = "local"\nprotocol = "openai"\nbase_url = "http://localhost"\n'
        '[[models]]\nname = "m"\nidentifier = "m"\nprovider = "local"\n'
        'reasoning_levels = ["high"]\n[organs.replyer]\n'
        'models = ["m"]\nreasoning_effort = "high"\n',
        encoding="utf-8",
    )
    before = runtime.read_bytes(), models.read_bytes()
    assert rig.client.post(RUNTIME_RESTORE, json={"field": "web.port"}).status_code == 403
    assert rig.client.post(RUNTIME_RESTORE, json={"field": ".config_version"}).status_code == 422
    assert rig.client.post(RUNTIME_RESTORE, json={"field": "web.session_days"}).status_code == 403
    assert rig.client.post(RUNTIME_RESTORE, json={"field": "identity.name"}).status_code == 422
    address = {"section": "providers", "name": "local", "field": "base_url"}
    assert rig.client.post(MODEL_RESTORE, json=address).status_code == 403
    assert (
        rig.client.post(MODEL_RESTORE, json={**address, "field": "api_key_ref"}).status_code == 422
    )
    assert (
        rig.client.post(
            MODEL_RESTORE, json={"section": "models", "name": "m", "field": "reasoning_levels"}
        ).status_code
        == 422
    )
    assert (runtime.read_bytes(), models.read_bytes()) == before
    assert (
        rig.client.post(MODEL_RESTORE, json={**address, "password": GOOD}).json()["value"] is None
    )
    assert (
        rig.client.post(
            RUNTIME_RESTORE, json={"field": "web.session_days", "password": GOOD}
        ).status_code
        == 200
    )
    assert tomllib.loads(runtime.read_text(encoding="utf-8"))["web"] == {"port": 30000}


def test_restore_requires_login_and_non_default_password(tmp_path: Path) -> None:
    rig = ConfigRig(tmp_path, password_set=False)
    try:
        for path in (RUNTIME_RESTORE, MODEL_RESTORE):
            assert rig.client.post(path, json={}).status_code == 401
        rig.log_in("admin")
        for path in (RUNTIME_RESTORE, MODEL_RESTORE):
            assert rig.client.post(path, json={}).status_code == 403
    finally:
        rig.close()
