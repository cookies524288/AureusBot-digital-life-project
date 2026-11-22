"""运行配置接口：按字段保存、只读项优先拒绝、敏感节确认密码（D84）。"""

import logging
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aureus.common import RuntimeConfig
from aureus.web.config_files import ConfigService
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import GOOD

ORIGINAL = """# 用户保留的说明
config_version = 1
log_level = "INFO"  # 保留行尾注释

[diary]
# write_after_hour = 22
max_chars = 1500  # 别的字段保持原样

[heartbeat]
tick_interval_seconds = 60.0  # 别的节保持原样
"""


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[ConfigRig]:
    made = ConfigRig(tmp_path)
    made.log_in()
    yield made
    made.close()


def save_original(rig: ConfigRig) -> None:
    rig.paths.runtime_config_file.write_bytes(ORIGINAL.encode("utf-8"))


@pytest.mark.parametrize("method", ["get", "put"])
def test_runtime_routes_require_login_and_a_changed_password(tmp_path: Path, method: str) -> None:
    rig = ConfigRig(tmp_path, password_set=False)
    try:
        assert rig.client.request(method, "/api/config", json={}).status_code == 401
        rig.log_in("admin")
        assert rig.client.request(method, "/api/config", json={}).status_code == 403
    finally:
        rig.close()


def test_missing_config_returns_defaults_schema_readonly_and_password_sections(
    rig: ConfigRig,
) -> None:
    data = rig.client.get("/api/config").json()
    assert data["exists"] is False
    assert data["config"] == RuntimeConfig().model_dump(mode="json")
    assert data["schema"] == RuntimeConfig.model_json_schema()
    assert "$defs" in data["schema"]
    assert data["readonly"] == [
        "config_version",
        "web.enabled",
        "web.allow_remote",
        "web.port",
        "qq.enabled",
        "qq.endpoint",
        "qq.account_id",
        "qq.token_key",
        "qq.action_timeout_seconds",
        "qq.reconnect_seconds",
    ]
    assert data["password_sections"] == ["safety", "web"]


@pytest.mark.parametrize(
    "changes",
    [
        {"config_version": 2},
        {"web": {"enabled": False}},
        {"web": {"allow_remote": True}},
        {"web": {"port": 12345}},
    ],
)
def test_readonly_changes_are_rejected_before_password_checks_and_do_not_consume_limits(
    rig: ConfigRig, changes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    save_original(rig)
    confirmations: list[bool] = []
    original = rig.auth.confirm_password

    def confirm(*args: Any) -> None:
        confirmations.append(True)
        original(*args)

    monkeypatch.setattr(rig.auth, "confirm_password", confirm)
    for _ in range(8):
        response = rig.client.put("/api/config", json={"changes": changes, "password": "wrong"})
        assert response.status_code == 403
        assert "只能在本机直接编辑" in response.json()["detail"]
        assert rig.paths.runtime_config_file.read_bytes() == ORIGINAL.encode("utf-8")
    assert confirmations == []
    assert (
        rig.client.put(
            "/api/config", json={"changes": {"web": {"session_days": 9}}, "password": GOOD}
        ).status_code
        == 200
    )
    assert confirmations == [True]


@pytest.mark.parametrize(
    "field, value",
    [
        ("enabled", True),
        ("endpoint", "ws://127.0.0.1:9000/onebot"),
        ("account_id", "12345"),
        ("token_key", "AUREUS_KEY_QQ_TEST"),
        ("action_timeout_seconds", 12.0),
        ("reconnect_seconds", 6.0),
    ],
)
def test_qq_page_settings_reject_edit_and_restore_without_touching_config(
    rig: ConfigRig, field: str, value: Any
) -> None:
    save_original(rig)
    path = rig.paths.runtime_config_file
    before = path.read_bytes()
    key = f"qq.{field}"

    edited = rig.client.put("/api/config", json={"changes": {"qq": {field: value}}})
    restored = rig.client.post("/api/config/restore-default", json={"field": key})

    assert edited.status_code == 403
    assert restored.status_code == 403
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes", [{"safety": {"max_calls_per_minute": 21}}, {"web": {"session_days": 9}}]
)
def test_sensitive_sections_require_the_current_password(
    rig: ConfigRig, changes: dict[str, Any]
) -> None:
    for password in (None, "wrong"):
        response = rig.client.put("/api/config", json={"changes": changes, "password": password})
        assert response.status_code == 403
        assert response.json()["detail"] == "密码不对。"
        assert not rig.paths.runtime_config_file.exists()
    assert (
        rig.client.put("/api/config", json={"changes": changes, "password": GOOD}).status_code
        == 200
    )


def test_sensitive_confirmations_are_rate_limited(rig: ConfigRig) -> None:
    body = {"changes": {"safety": {"max_calls_per_minute": 21}}, "password": "wrong"}
    for _ in range(3):
        assert rig.client.put("/api/config", json=body).status_code == 403
    body["password"] = GOOD
    response = rig.client.put("/api/config", json=body)
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_unchanged_sensitive_and_readonly_fields_are_ignored_before_confirmation(
    rig: ConfigRig,
) -> None:
    save_original(rig)
    config = rig.client.get("/api/config").json()["config"]
    before = rig.paths.runtime_config_file.stat().st_mtime_ns
    response = rig.client.put(
        "/api/config",
        json={
            "changes": {
                "web": config["web"],
                "safety": {"max_calls_per_minute": config["safety"]["max_calls_per_minute"]},
                "config_version": config["config_version"],
            },
            "password": "wrong",
        },
    )
    assert response.json() == {"ok": True, "changed": [], "restart_required": False}
    assert rig.paths.runtime_config_file.stat().st_mtime_ns == before
    assert rig.paths.runtime_config_file.read_bytes() == ORIGINAL.encode("utf-8")


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": {"value": 1}},
        {"diary": {"unknown": 1}},
        {"diary": {"write_after_hour": 99}},
        {"diary": {"write_after_hour": "21"}},
        {"diary": {"write_after_hour": True}},
        {"diary": {"write_after_hour": 22.0}},
        {"exploration": {"enabled": True}, "interests": {"enabled": False}},
        {"log_level": {"wrong": 1}},
        {"diary": False},
    ],
)
def test_invalid_runtime_config_is_rejected_without_file_changes(
    rig: ConfigRig, changes: dict[str, Any]
) -> None:
    save_original(rig)
    response = rig.client.put("/api/config", json={"changes": changes})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert str(rig.paths.home) not in response.text
    assert rig.paths.runtime_config_file.read_bytes() == ORIGINAL.encode("utf-8")
    assert rig.service.restart_required is False


@pytest.mark.parametrize(
    "changes",
    [
        {"log_level": None},
        {"log_level": []},
        {"diary": {"write_after_hour": None}},
        {"diary": {"write_after_hour": [21]}},
        {"diary": {"write_after_hour": {"nested": 21}}},
    ],
)
def test_only_scalar_values_and_one_section_level_are_accepted(
    rig: ConfigRig, changes: dict[str, Any]
) -> None:
    response = rig.client.put("/api/config", json={"changes": changes})
    assert response.status_code == 422
    assert response.json()["detail"] == "配置的值只能是开关、数字或文字。"
    assert not rig.paths.runtime_config_file.exists()


def test_save_keeps_comments_and_other_sections_and_reports_changed_fields(rig: ConfigRig) -> None:
    save_original(rig)
    response = rig.client.put("/api/config", json={"changes": {"diary": {"write_after_hour": 21}}})
    assert response.json() == {
        "ok": True,
        "changed": ["diary.write_after_hour"],
        "restart_required": True,
    }
    text = rig.paths.runtime_config_file.read_text(encoding="utf-8")
    assert ORIGINAL.split("[diary]")[0] in text
    assert ORIGINAL.split("[heartbeat]")[1] in text
    assert "# write_after_hour = 22" in text
    assert "max_chars = 1500  # 别的字段保持原样" in text
    assert text.index("max_chars") < text.index("write_after_hour = 21") < text.index("[heartbeat]")
    assert rig.client.get("/api/config").json()["config"]["diary"]["write_after_hour"] == 21
    assert rig.client.get("/api/overview").json()["restart_required"] is True
    before = rig.paths.runtime_config_file.stat().st_mtime_ns
    assert rig.client.put(
        "/api/config", json={"changes": {"diary": {"write_after_hour": 21}}}
    ).json() == {
        "ok": True,
        "changed": [],
        "restart_required": True,
    }
    assert rig.paths.runtime_config_file.stat().st_mtime_ns == before


def test_missing_sections_are_created_and_logs_only_contain_field_names(
    rig: ConfigRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    response = rig.client.put(
        "/api/config",
        json={"changes": {"time": {"timezone": "Asia/Shanghai"}, "log_level": "DEBUG"}},
    )
    assert response.json()["changed"] == ["time.timezone", "log_level"]
    raw = tomllib.loads(rig.paths.runtime_config_file.read_text(encoding="utf-8"))
    assert raw["time"]["timezone"] == "Asia/Shanghai"
    assert raw["log_level"] == "DEBUG"
    messages = "\n".join(
        record.message for record in caplog.records if record.name == "aureus.web.config_files"
    )
    assert "time.timezone" in messages and "log_level" in messages
    assert "Asia/Shanghai" not in messages and "DEBUG" not in messages
    assert rig.timeline.count_records() == 0
    assert ConfigService(rig.paths).restart_required is False  # 新进程自然清零。


@pytest.mark.parametrize("content", ["[diary", "[diary]\nwrite_after_hour = 99"])
def test_broken_file_returns_422_without_a_local_path(rig: ConfigRig, content: str) -> None:
    rig.paths.runtime_config_file.write_text(content, encoding="utf-8")
    response = rig.client.get("/api/config")
    assert response.status_code == 422
    assert "aureus.toml" in response.json()["detail"]
    assert str(rig.paths.home) not in response.text


def test_write_failure_preserves_bytes_and_restart_flag(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_original(rig)

    def fail(*args: Any) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr("aureus.common.toml_edit.os.replace", fail)
    response = rig.client.put("/api/config", json={"changes": {"log_level": "DEBUG"}})
    assert response.status_code == 500
    assert response.json()["detail"] == "写不进配置文件。"
    assert rig.paths.runtime_config_file.read_bytes() == ORIGINAL.encode("utf-8")
    assert rig.service.restart_required is False
    assert list(rig.paths.config_dir.glob("*.tmp")) == []
