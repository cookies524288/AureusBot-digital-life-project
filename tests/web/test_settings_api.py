# 设置页接口的测试：登录要求、口令确认、保存（路线图 1.10，D63 第 1、2、4 条）。
# 身份写回、版本历史和口令都是真的，心跳是假的，不开真实端口。

import tomllib
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.common import ManualClock, WebConfig
from aureus.heartbeat import Perception
from aureus.identity import Identity, IdentityEditor, IdentityHistory, IdentityLayer
from aureus.timeline import TimelineDatabase
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app
from aureus.web.settings import SettingsService

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
GOOD = "correct horse"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

ORIGINAL = """[facts]
name = "小绿"
alias_names = []
gender = ""
birthday = "2008-01-01"

[user]
name = "阿明"
accounts = []

[persona]
immutable = "温和。"
mutable = ""
behavior_style = ""
expression_style = "句子短。"

[persona.locks]
mutable = false
behavior_style = false
expression_style = false
"""


class FakeHeartbeat:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.applied: list[tuple[tuple[IdentityLayer, ...], str]] = []

    def submit(self, perception: Perception) -> bool:
        return True

    def apply_identity_edit(
        self,
        identity: Identity,
        layers: Sequence[IdentityLayer],
        *,
        up_to_version_id: int,
        via: str,
    ) -> None:
        self.identity = identity
        self.applied.append((tuple(layers), via))


class Rig:
    def __init__(
        self, tmp_path: Path, *, password_set: bool = True, max_failures: int = 10
    ) -> None:
        clock = ManualClock(NOW)
        config = WebConfig(login_max_failures=max_failures)
        self.identity_file = tmp_path / "identity.toml"
        self.identity_file.write_bytes(ORIGINAL.encode("utf-8"))
        self.history = IdentityHistory(tmp_path / "aureus.db", clock)
        self.history.open()
        from_file = Identity.model_validate(tomllib.loads(ORIGINAL))
        self.heartbeat = FakeHeartbeat(self.history.sync_from_file(from_file))
        passwords = PasswordStore(tmp_path / "web_password.argon2", FAST_HASHER)
        if password_set:
            passwords.set_password(GOOD)
        self.sessions = SessionStore(tmp_path / "aureus.db", clock, config.session_days)
        self.sessions.open()
        self.timeline = TimelineDatabase(tmp_path / "aureus.db", clock)
        self.timeline.open()
        auth = WebAuth(passwords, self.sessions, LoginLimiter(config, clock))
        settings = SettingsService(
            editor=IdentityEditor(self.identity_file, self.history),
            history=self.history,
            heartbeat=self.heartbeat,
            auth=auth,
        )
        app = create_app(
            config=config,
            auth=auth,
            sessions=self.sessions,
            channel=WebChannel(self.heartbeat),
            timeline=self.timeline,
            settings=settings,
        )
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def log_in(self, password: str = GOOD) -> None:
        response = self.client.post("/api/login", json={"password": password})
        assert response.status_code == 200

    def file_text(self) -> str:
        return self.identity_file.read_bytes().decode("utf-8")

    def close(self) -> None:
        self.history.close()
        self.sessions.close()
        self.timeline.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


def test_the_settings_api_needs_a_login(rig: Rig) -> None:
    assert rig.client.get("/api/settings").status_code == 401
    assert rig.client.put("/api/settings", json={}).status_code == 401
    rollback = {"layer": "mutable", "version_id": 1}
    assert rig.client.post("/api/settings/rollback", json=rollback).status_code == 401
    assert rig.file_text() == ORIGINAL


def test_the_settings_api_is_blocked_while_the_password_is_the_default(tmp_path: Path) -> None:
    rig = Rig(tmp_path, password_set=False)
    try:
        rig.log_in("admin")
        assert rig.client.get("/api/settings").status_code == 403
        assert rig.client.put("/api/settings", json={"text": {"mutable": "x"}}).status_code == 403
        assert rig.file_text() == ORIGINAL
        assert rig.heartbeat.applied == []
    finally:
        rig.close()


def test_the_view_returns_the_current_settings(rig: Rig) -> None:
    rig.log_in()
    data = rig.client.get("/api/settings").json()

    assert data["layers"]["facts"]["facts"]["name"] == "小绿"
    assert data["layers"]["facts"]["user"]["name"] == "阿明"
    assert data["layers"]["immutable"].strip() == "温和。"
    assert data["layers"]["expression_style"].strip() == "句子短。"
    assert data["locks"] == {"mutable": False, "behavior_style": False, "expression_style": False}
    assert set(data["history"]) == {"mutable", "behavior_style", "expression_style"}


def test_a_free_layer_is_saved_without_the_password_and_takes_effect(rig: Rig) -> None:
    rig.log_in()
    response = rig.client.put("/api/settings", json={"text": {"expression_style": "多用语气词。"}})

    assert response.status_code == 200
    assert response.json() == {"changed": ["expression_style"]}
    assert "多用语气词。" in rig.file_text()
    assert rig.heartbeat.applied == [((IdentityLayer.EXPRESSION_STYLE,), "web")]


@pytest.mark.parametrize(
    "body",
    [
        {"text": {"immutable": "新底线。"}},
        {"text": {"immutable": "新底线。"}, "password": "wrong password"},
        {"facts": {"name": "小蓝", "birthday": "2008-01-01"}},
        {"user": {"name": "阿强"}},
    ],
)
def test_the_baseline_needs_the_right_password(rig: Rig, body: dict) -> None:
    rig.log_in()
    response = rig.client.put("/api/settings", json=body)

    # 403 而不是 401：401 会让页面以为登录过期、跳回登录页。
    assert response.status_code == 403
    assert rig.file_text() == ORIGINAL
    assert rig.heartbeat.applied == []
    assert rig.client.get("/api/status").status_code == 200  # 登录状态还在


def test_the_baseline_is_saved_with_the_right_password(rig: Rig) -> None:
    rig.log_in()
    body = {
        "password": GOOD,
        "text": {"immutable": "新底线。"},
        "facts": {"name": "小蓝", "alias_names": ["蓝蓝"], "birthday": "2009-02-03"},
        "user": {"name": "阿强", "accounts": [{"platform": "qq", "id": "10000"}]},
    }
    response = rig.client.put("/api/settings", json=body)

    assert response.status_code == 200
    assert set(response.json()["changed"]) == {"immutable", "facts"}
    data = tomllib.loads(rig.file_text())
    assert data["facts"]["name"] == "小蓝"
    assert data["facts"]["alias_names"] == ["蓝蓝"]
    assert data["user"]["accounts"] == [{"platform": "qq", "id": "10000"}]
    assert data["persona"]["immutable"].strip() == "新底线。"
    assert GOOD not in rig.file_text()  # 口令只用来核对，不写进任何文件
    assert rig.heartbeat.identity.facts.name == "小蓝"


def test_a_lock_toggle_is_saved(rig: Rig) -> None:
    rig.log_in()
    locks = {"mutable": True, "behavior_style": False, "expression_style": False}
    response = rig.client.put("/api/settings", json={"locks": locks})

    assert response.status_code == 200
    assert response.json() == {"changed": []}
    assert rig.client.get("/api/settings").json()["locks"] == locks
    assert "mutable = true" in rig.file_text()


@pytest.mark.parametrize(
    "locks",
    [
        {"mutable": "yes", "behavior_style": False, "expression_style": False},  # 字符串不是 true
        {"mutable": 1, "behavior_style": False, "expression_style": False},  # 数字 1 不是 true
        {"mutable": True},  # 漏写的开关不能悄悄当成 false，免得解开另外两层的锁
        {"mutable": True, "behavior_style": False, "expression_style": False, "extra": True},
    ],
)
def test_malformed_locks_are_refused_and_nothing_changes(rig: Rig, locks: dict) -> None:
    rig.log_in()
    response = rig.client.put("/api/settings", json={"locks": locks})

    assert response.status_code == 422
    assert rig.file_text() == ORIGINAL
    assert rig.heartbeat.applied == []


# ---------- 锁定开关 ----------


def test_all_three_locks_are_saved_and_written_back(rig: Rig) -> None:
    rig.log_in()
    locks = {"mutable": True, "behavior_style": False, "expression_style": True}
    response = rig.client.put("/api/settings", json={"locks": locks})

    assert response.status_code == 200
    assert response.json() == {"changed": []}  # 锁定开关不记版本
    assert tomllib.loads(rig.file_text())["persona"]["locks"] == locks
    assert rig.client.get("/api/settings").json()["locks"] == locks


@pytest.mark.parametrize(
    "locks",
    [
        {"mutable": True},  # 漏写的不能悄悄当成 false
        {"mutable": "yes", "behavior_style": False, "expression_style": False},
        {"mutable": 1, "behavior_style": False, "expression_style": False},
    ],
)
def test_incomplete_or_non_boolean_locks_are_refused(rig: Rig, locks: dict) -> None:
    rig.log_in()
    response = rig.client.put("/api/settings", json={"locks": locks})
    assert response.status_code == 422
    assert rig.file_text() == ORIGINAL


# ---------- 请求体校验 ----------


@pytest.mark.parametrize(
    "body",
    [
        {"mutable": "x"},  # 文字层要放在 text 下面，顶层不认
        {"text": {"nonsense": "x"}},
        {"text": {"mutable": "字" * 8001}},
        {"facts": {"name": "", "birthday": "2008-01-01"}},
        {"facts": {"name": "小蓝", "birthday": "二〇〇八年"}},
        {"facts": {"name": "小蓝", "birthday": "2008-02-30"}},
        {"user": {"name": "阿强", "accounts": [{"platform": "", "id": "1"}]}},
        {"password": "x" * 300},
    ],
)
def test_a_malformed_body_is_refused_and_nothing_changes(rig: Rig, body: dict) -> None:
    rig.log_in()
    response = rig.client.put("/api/settings", json=body)

    assert response.status_code == 422
    assert rig.file_text() == ORIGINAL
    assert rig.heartbeat.applied == []


def test_an_empty_immutable_persona_is_refused(rig: Rig) -> None:
    rig.log_in()
    body = {"password": GOOD, "text": {"immutable": "   "}}
    response = rig.client.put("/api/settings", json=body)

    assert response.status_code == 422
    assert "不变人格不能是空的" in response.json()["detail"]
    assert rig.file_text() == ORIGINAL


def test_the_error_never_echoes_the_password(rig: Rig) -> None:
    rig.log_in()
    body = {"password": "SECRET-guess", "text": {"immutable": "新底线。"}}
    response = rig.client.put("/api/settings", json=body)
    assert response.status_code == 403
    assert "SECRET" not in response.text


def test_repeated_wrong_passwords_for_the_baseline_end_in_a_lockout(tmp_path: Path) -> None:
    rig = Rig(tmp_path, max_failures=3)
    try:
        rig.log_in()
        body = {"password": "wrong password", "text": {"immutable": "新底线。"}}
        for _ in range(3):
            assert rig.client.put("/api/settings", json=body).status_code == 403
        locked = rig.client.put(
            "/api/settings", json={"password": GOOD, "text": {"immutable": "新底线。"}}
        )
        assert locked.status_code == 429
        assert int(locked.headers["retry-after"]) >= 1
        assert rig.file_text() == ORIGINAL
    finally:
        rig.close()


# ---------- 回退 ----------


def save_style(rig: Rig, text: str) -> None:
    response = rig.client.put("/api/settings", json={"text": {"expression_style": text}})
    assert response.status_code == 200


def test_rolling_back_restores_an_older_version_as_a_new_one(rig: Rig) -> None:
    rig.log_in()
    for number in range(3):
        save_style(rig, f"第{number}版。")
    history = rig.client.get("/api/settings").json()["history"]["expression_style"]
    target = history[0]  # 最近 3 个里最早的

    response = rig.client.post(
        "/api/settings/rollback", json={"layer": "expression_style", "version_id": target["id"]}
    )

    assert response.status_code == 200
    assert response.json() == {"changed": ["expression_style"]}
    assert target["content"] in rig.file_text()
    after = rig.client.get("/api/settings").json()["history"]["expression_style"]
    assert after[-1]["content"] == target["content"]  # 多了一个新版本，旧的都还在


def test_rolling_back_to_a_version_that_is_too_old_is_refused(rig: Rig) -> None:
    rig.log_in()
    for number in range(5):
        save_style(rig, f"第{number}版。")
    oldest = rig.history.versions(IdentityLayer.EXPRESSION_STYLE, limit=100)[0]
    response = rig.client.post(
        "/api/settings/rollback", json={"layer": "expression_style", "version_id": oldest.id}
    )
    assert response.status_code == 422
    assert "最近的几个版本" in response.json()["detail"]


@pytest.mark.parametrize("layer", ["facts", "immutable"])
def test_the_baseline_layers_cannot_be_rolled_back_through_this_api(rig: Rig, layer: str) -> None:
    rig.log_in()
    version = rig.history.versions(IdentityLayer(layer), limit=1)[0]
    response = rig.client.post(
        "/api/settings/rollback", json={"layer": layer, "version_id": version.id}
    )
    assert response.status_code == 422
    assert rig.heartbeat.applied == []


@pytest.mark.parametrize(
    "body",
    [
        {"layer": "nonsense", "version_id": 1},
        {"layer": "mutable", "version_id": 0},
        {"layer": "mutable"},
        {"layer": "mutable", "version_id": 1, "extra": 1},
    ],
)
def test_a_malformed_rollback_is_refused(rig: Rig, body: dict) -> None:
    rig.log_in()
    assert rig.client.post("/api/settings/rollback", json=body).status_code == 422


# ---------- 首页 ----------


def test_the_index_page_opens_without_logging_in_and_holds_no_data(rig: Rig) -> None:
    response = rig.client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert "小绿" not in response.text  # 页面本身没有数据，不能写死示例内容
    assert "阿明" not in response.text
