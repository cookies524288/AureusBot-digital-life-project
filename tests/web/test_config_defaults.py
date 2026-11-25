"""通过真实网页配置接口和启动加载器验收逐项恢复（D91）。"""

import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, RuntimeConfig, load_runtime_config, shutdown_logging
from aureus.core.runtime import open_core
from aureus.organs import load_models_config
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import GOOD
from tests.web.test_web_acceptance_flow import IDENTITY

RUNTIME_RESTORE = "/api/config/restore-default"
RUNTIME_RESTORE_ALL = "/api/config/restore-defaults"
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
        "memory.recall_feedback_enabled",
        "memory.removal_operations_enabled",
        "memory.max_results",
        "memory.index_batch",
        "memory.consolidation_enabled",
        "memory.consolidation_extract_enabled",
        "memory.consolidation_batch",
        "memory.consolidation_half_life_days",
        "language.recall_enabled",
        "language.reference_chars",
    ],
)
def test_language_and_memory_restore_preserves_other_overrides(rig: ConfigRig, field: str) -> None:
    changes = {
        "memory": {
            "enabled": False,
            "recall_enabled": False,
            "heuristic_recall_enabled": True,
            "recall_feedback_enabled": False,
            "removal_operations_enabled": False,
            "max_results": 12,
            "index_batch": 321,
            "consolidation_enabled": True,
            "consolidation_extract_enabled": True,
            "consolidation_batch": 32,
            "consolidation_half_life_days": 15.0,
        },
        "language": {
            "recall_enabled": False,
            "reference_chars": 1500,
        },
    }
    client = rig.client
    assert client.put("/api/config", json={"changes": changes}).status_code == 200
    path = rig.paths.runtime_config_file
    assert tomllib.loads(path.read_text(encoding="utf-8")) == changes
    section, name = field.split(".")
    defaults = RuntimeConfig().model_dump()
    default = defaults[section][name]
    assert changes[section].pop(name) != default
    restored = client.post(RUNTIME_RESTORE, json={"field": field})
    assert restored.status_code == 200
    assert restored.json() == {"value": default, "changed": True, "restart_required": True}
    assert tomllib.loads(path.read_text(encoding="utf-8")) == changes
    page_config = client.get("/api/config").json()["config"]
    loaded = load_runtime_config(rig.paths).model_dump()
    for group, values in changes.items():
        expected = {**defaults[group], **values}
        assert page_config[group] == expected
        assert loaded[group] == expected


def test_legacy_language_learning_setting_is_not_visible_or_restorable(rig: ConfigRig) -> None:
    path = rig.paths.runtime_config_file
    original = "[language]\nlearning_enabled = false\nreference_chars = 1000\n"
    path.write_text(original, encoding="utf-8")
    client = rig.client
    setting = "language.learning_enabled"
    view = client.get("/api/config").json()
    assert "learning_enabled" not in view["config"]["language"]
    assert "learning_enabled" not in view["schema"]["$defs"]["LanguageConfig"]["properties"]
    assert (
        client.put(
            "/api/config", json={"changes": {"language": {"learning_enabled": True}}}
        ).status_code
        == 403
    )
    assert client.post(RUNTIME_RESTORE, json={"field": setting}).status_code == 403
    assert path.read_text(encoding="utf-8") == original


def test_model_edit_preserves_sparse_values_and_has_no_restore(rig: ConfigRig) -> None:
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

    before = path.read_bytes()
    for section, name, field in (
        ("providers", "local", "timeout_seconds"),
        ("models", "renamed", "temperature"),
        ("organs", "replyer", "models"),
    ):
        params = {"section": section, "name": name, "field": field}
        assert rig.client.post(MODEL_RESTORE, json=params).status_code == 404
    assert path.read_bytes() == before
    loaded = load_models_config(rig.paths)
    assert loaded.providers[0].timeout_seconds == 21.0
    assert loaded.providers[0].max_retry == 7
    assert loaded.routes_for("replyer")[0].max_tokens == 200


def test_restore_permissions_unknown_keys_and_invalid_result_keep_files(rig: ConfigRig) -> None:
    runtime = rig.paths.runtime_config_file
    runtime.write_text("[web]\nport = 30000\nsession_days = 12\n", encoding="utf-8")
    before = runtime.read_bytes()
    assert rig.client.post(RUNTIME_RESTORE, json={"field": "web.port"}).status_code == 403
    assert rig.client.post(RUNTIME_RESTORE, json={"field": ".config_version"}).status_code == 422
    assert rig.client.post(RUNTIME_RESTORE, json={"field": "web.session_days"}).status_code == 403
    assert rig.client.post(RUNTIME_RESTORE, json={"field": "identity.name"}).status_code == 422
    assert runtime.read_bytes() == before
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
        for endpoint in (RUNTIME_RESTORE, RUNTIME_RESTORE_ALL):
            assert rig.client.post(endpoint, json={}).status_code == 401
        rig.log_in("admin")
        for endpoint in (RUNTIME_RESTORE, RUNTIME_RESTORE_ALL):
            assert rig.client.post(endpoint, json={}).status_code == 403
    finally:
        rig.close()


def test_restore_all_requires_password_and_honors_the_shared_limit(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = rig.paths.runtime_config_file
    before = b'log_level = "DEBUG"\n'
    path.write_bytes(before)

    def reject_touch(_token: str) -> bool:
        pytest.fail("加载设置和执行总重置不应续期或清理登录状态")

    with monkeypatch.context() as context:
        context.setattr(rig.sessions, "touch", reject_touch)
        assert rig.client.get("/api/config").status_code == 200
        for body in ({}, {"password": "wrong"}, {"password": "wrong"}):
            response = rig.client.post(RUNTIME_RESTORE_ALL, json=body)
            assert response.status_code == 403
            assert response.json()["detail"] == "密码不对。"
            assert path.read_bytes() == before
        response = rig.client.post(RUNTIME_RESTORE_ALL, json={"password": GOOD})
        assert response.status_code == 429 and "Retry-After" in response.headers
    assert (
        rig.client.post(
            RUNTIME_RESTORE, json={"field": "web.session_days", "password": GOOD}
        ).status_code
        == 429
    )
    assert path.read_bytes() == before
    assert not rig.service.restart_required


def test_restore_all_removes_only_settings_overrides(rig: ConfigRig) -> None:
    paths = rig.paths
    kept_files = {
        paths.identity_file: IDENTITY.encode("utf-8"),
        paths.models_config_file: b'# synthetic model settings\n[[providers]]\nname = "sample"\n',
        paths.home / "prompts.toml": b"# synthetic prompt settings\n",
        paths.data_dir / "sample.txt": b"synthetic data",
    }
    for path, content in kept_files.items():
        path.write_bytes(content)
    paths.runtime_config_file.write_bytes(
        b'# keep this note\nconfig_version = 1\nlog_level = "DEBUG"\n'
        b'[heartbeat]\nadvance_biomodel = true\nplan_organ = "old"\n'
        b"inbox_read_limit = 72\n[cortisol]\nenabled = true\n"
        b"[hormones]\nanger_half_life_seconds = 731.0 # keep duration\njoy_enabled = false\n"
        b"[mood]\nhalf_life_seconds = 18000.0\nenabled = false\n"
        b'[exploration]\nsearch_endpoint = "https://search.example.test"\n'
        b"[web]\nport = 30000 # keep this comment\nsession_days = 12\n"
        b"[safety]\nmax_calls_per_minute = 21\n"
    )
    response = rig.client.post(
        RUNTIME_RESTORE_ALL, json={"password": GOOD, "changes": {"web": {"port": 12345}}}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["changed"] == [
        "heartbeat.inbox_read_limit",
        "hormones.joy_enabled",
        "log_level",
        "mood.enabled",
        "safety.max_calls_per_minute",
        "web.session_days",
    ]
    assert data["restart_required"] is True
    assert data["config"] == rig.client.get("/api/config").json()["config"]
    assert "advance_biomodel" not in data["config"]["heartbeat"]
    assert data["config"]["log_level"] == RuntimeConfig().log_level
    assert data["config"]["heartbeat"]["inbox_read_limit"] == HeartbeatConfig().inbox_read_limit
    text = paths.runtime_config_file.read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    assert raw.pop("safety", {}) == {}
    assert raw == {
        "config_version": 1,
        "heartbeat": {"advance_biomodel": True, "plan_organ": "old"},
        "cortisol": {"enabled": True},
        "hormones": {"anger_half_life_seconds": 731.0},
        "mood": {"half_life_seconds": 18000.0},
        "exploration": {"search_endpoint": "https://search.example.test"},
        "web": {"port": 30000},
    }
    assert "# keep this note" in text and "# keep this comment" in text
    assert "anger_half_life_seconds = 731.0 # keep duration" in text
    assert all(path.read_bytes() == content for path, content in kept_files.items())
    assert rig.timeline.count_records() == 0
    rig.auth.confirm_password(GOOD, "127.0.0.1")


def test_restore_all_with_nothing_to_restore_does_not_create_a_file(rig: ConfigRig) -> None:
    response = rig.client.post(RUNTIME_RESTORE_ALL, json={"password": GOOD})
    assert response.status_code == 200
    assert response.json()["changed"] == []
    assert response.json()["restart_required"] is False
    assert not rig.paths.runtime_config_file.exists()


def test_restore_all_disk_failure_keeps_every_override(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = rig.paths.runtime_config_file
    before = b'log_level = "DEBUG"\n[web]\nsession_days = 12\n'
    path.write_bytes(before)

    def fail_replace(_source: str, _destination: Path) -> None:
        raise OSError("synthetic replacement error")

    monkeypatch.setattr("aureus.common.toml_edit.os.replace", fail_replace)
    response = rig.client.post(RUNTIME_RESTORE_ALL, json={"password": GOOD})
    assert response.status_code == 500
    assert path.read_bytes() == before
    assert not rig.service.restart_required
    assert list(rig.paths.config_dir.glob("*.tmp")) == []


def test_web_keeps_legacy_settings_off_the_page_and_preserves_file(rig: ConfigRig) -> None:
    path = rig.paths.runtime_config_file
    legacy = {
        "advance_biomodel": True,
        "planning_enabled": False,
        "roleplay_mode": True,
        "plan_organ": "custom-plan",
        "reply_organ": "custom-reply",
    }
    legacy_toml = (
        "[heartbeat]\nadvance_biomodel = true\nplanning_enabled = false\nroleplay_mode = true\n"
        'plan_organ = "custom-plan"\nreply_organ = "custom-reply"\n'
        '[exploration]\nsearch_endpoint = "https://search.example.test"\n'
    )
    path.write_bytes(legacy_toml.encode("utf-8"))
    view = rig.client.get("/api/config").json()
    assert not set(legacy).intersection(view["config"]["heartbeat"])
    assert not set(legacy).intersection(view["schema"]["$defs"]["HeartbeatConfig"]["properties"])
    assert "exploration.search_endpoint" in view["no_default"]
    assert "safety.monthly_token_limit" not in view["no_default"]
    for field, value in legacy.items():
        changes = {"heartbeat": {field: value}}
        assert rig.client.put("/api/config", json={"changes": changes}).status_code == 403
        response = rig.client.post(RUNTIME_RESTORE, json={"field": f"heartbeat.{field}"})
        assert response.status_code == 403
    response = rig.client.post(RUNTIME_RESTORE, json={"field": "exploration.search_endpoint"})
    assert response.status_code == 422
    assert path.read_bytes() == legacy_toml.encode("utf-8")

    changes = {"heartbeat": {"inbox_debounce_enabled": False}}
    assert rig.client.put("/api/config", json={"changes": changes}).status_code == 200
    assert load_runtime_config(rig.paths).heartbeat.inbox_debounce_enabled is False
    response = rig.client.post(RUNTIME_RESTORE, json={"field": "heartbeat.inbox_debounce_enabled"})
    assert response.status_code == 200 and response.json()["value"] is True
    assert load_runtime_config(rig.paths).heartbeat.inbox_debounce_enabled is True
    assert tomllib.loads(path.read_text(encoding="utf-8")) == tomllib.loads(legacy_toml)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("heartbeat", "qq_emoji_semantic_enabled", False),
        ("language", "use_vector_expression", False),
        ("judge", "organ", "old-judge"),
        ("exploration", "organ", "old-explorer"),
        ("language", "organ", "old-learner"),
        ("language", "expression_quality_organ", "old-reviewer"),
        ("language", "expression_selector_organ", "old-selector"),
        ("diary", "organ", "old-writer"),
        ("persona_growth", "organ", "old-growth"),
    ],
)
def test_d107_legacy_settings_hidden_and_not_writable(
    rig: ConfigRig, section: str, field: str, value: str | bool
) -> None:
    path = rig.paths.runtime_config_file
    raw_value = f'"{value}"' if isinstance(value, str) else str(value).lower()
    before = f"[{section}]\n{field} = {raw_value}\n".encode()
    path.write_bytes(before)
    client = rig.client
    view = client.get("/api/config").json()
    schema = view["schema"]
    definition = schema["properties"][section]["$ref"].rsplit("/", 1)[-1]
    assert field not in view["config"][section]
    assert field not in schema["$defs"][definition]["properties"]

    replacement = "new-organ" if isinstance(value, str) else not value
    for changes in ({section: {field: replacement}}, {f"{section}.{field}": replacement}):
        assert client.put("/api/config", json={"changes": changes}).status_code == 403
    assert client.post(RUNTIME_RESTORE, json={"field": f"{section}.{field}"}).status_code == 403
    assert path.read_bytes() == before
    assert not rig.service.restart_required
