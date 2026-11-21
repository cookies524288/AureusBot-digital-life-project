"""设置页服务层的测试：口令确认、写回、热更新心跳、回退（路线图 1.10，D63 第 1、2、4、5 条）。"""

import tomllib
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from aureus.common import ManualClock, WebConfig
from aureus.identity import (
    Facts,
    Identity,
    IdentityEdit,
    IdentityEditError,
    IdentityEditor,
    IdentityHistory,
    IdentityLayer,
    PersonaLocks,
    VersionSource,
)
from aureus.web import (
    LoginLimiter,
    LoginLockedError,
    LoginRejectedError,
    PasswordStore,
    SessionStore,
    WebAuth,
)
from aureus.web.settings import ROLLBACK_CHOICES, SettingsService

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
GOOD = "correct horse"
LOCAL = "127.0.0.1"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

ORIGINAL = """# 注释别弄丢
[facts]
name = "小绿"
alias_names = []
gender = ""
birthday = "2008-01-01"

[user]
name = "阿明"
accounts = []

[persona]
immutable = \"\"\"
温和。
\"\"\"
mutable = \"\"\"
\"\"\"
behavior_style = \"\"\"
\"\"\"
expression_style = \"\"\"
句子短。
\"\"\"

[persona.locks]
mutable = false
behavior_style = false
expression_style = false
"""


class FakeHeartbeat:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.applied: list[tuple[tuple[IdentityLayer, ...], int, str]] = []

    def apply_identity_edit(
        self,
        identity: Identity,
        layers: Sequence[IdentityLayer],
        *,
        up_to_version_id: int,
        via: str,
    ) -> None:
        self.identity = identity
        self.applied.append((tuple(layers), up_to_version_id, via))


class Rig:
    def __init__(self, tmp_path: Path, *, max_failures: int = 10) -> None:
        clock = ManualClock(NOW)
        self.identity_file = tmp_path / "identity.toml"
        self.identity_file.write_bytes(ORIGINAL.encode("utf-8"))
        self.history = IdentityHistory(tmp_path / "aureus.db", clock)
        self.history.open()
        from_file = Identity.model_validate(tomllib.loads(ORIGINAL))
        effective = self.history.sync_from_file(from_file)
        self.heartbeat = FakeHeartbeat(effective)
        passwords = PasswordStore(tmp_path / "web_password.argon2", FAST_HASHER)
        passwords.set_password(GOOD)
        self.sessions = SessionStore(tmp_path / "aureus.db", clock, 7)
        self.sessions.open()
        self.auth = WebAuth(
            passwords,
            self.sessions,
            LoginLimiter(WebConfig(login_max_failures=max_failures), clock),
        )
        self.service = SettingsService(
            editor=IdentityEditor(self.identity_file, self.history),
            history=self.history,
            heartbeat=self.heartbeat,
            auth=self.auth,
        )

    def close(self) -> None:
        self.history.close()
        self.sessions.close()

    def file_text(self) -> str:
        return self.identity_file.read_bytes().decode("utf-8")


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


STYLE = IdentityLayer.EXPRESSION_STYLE
IMMUTABLE = IdentityLayer.IMMUTABLE


# ---------- 看设定 ----------


def test_the_view_shows_five_layers_locks_and_recent_versions(rig: Rig) -> None:
    view = rig.service.view()

    assert view["layers"]["facts"]["facts"]["name"] == "小绿"
    assert view["layers"]["facts"]["user"]["name"] == "阿明"
    assert view["layers"]["immutable"].strip() == "温和。"
    assert view["layers"]["expression_style"].strip() == "句子短。"
    assert view["locks"] == {"mutable": False, "behavior_style": False, "expression_style": False}
    assert [v["source"] for v in view["history"]["expression_style"]] == ["初始版本"]
    assert set(view["history"]) == {"mutable", "behavior_style", "expression_style"}


def test_the_view_follows_a_live_edit(rig: Rig) -> None:
    rig.service.apply(IdentityEdit(text={STYLE: "慢一点说。"}), password=None, remote_addr=LOCAL)
    assert rig.service.view()["layers"]["expression_style"].strip() == "慢一点说。"


# ---------- 改设定 ----------


def test_a_free_layer_needs_no_password_and_takes_effect_at_once(rig: Rig) -> None:
    result = rig.service.apply(
        IdentityEdit(text={STYLE: "多用语气词。"}), password=None, remote_addr=LOCAL
    )

    assert result.changed_layers == (STYLE,)
    assert "多用语气词。" in rig.file_text()
    assert "# 注释别弄丢" in rig.file_text()
    layers, version, via = rig.heartbeat.applied[0]
    assert (layers, via) == ((STYLE,), "web")
    assert version == result.up_to_version_id
    assert rig.heartbeat.identity.persona.expression_style.strip() == "多用语气词。"


def test_the_immutable_persona_needs_the_current_password(rig: Rig) -> None:
    edit = IdentityEdit(text={IMMUTABLE: "新底线。"})
    for wrong in (None, "", "wrong password"):
        with pytest.raises(LoginRejectedError):
            rig.service.apply(edit, password=wrong, remote_addr=LOCAL)

    assert rig.file_text() == ORIGINAL  # 文件没动
    assert rig.heartbeat.applied == []  # 心跳没动

    rig.service.apply(edit, password=GOOD, remote_addr=LOCAL)
    assert rig.heartbeat.identity.persona.immutable.strip() == "新底线。"


def test_facts_need_the_password_too(rig: Rig) -> None:
    edit = IdentityEdit(facts=Facts(name="小蓝", birthday="2008-01-01"))
    with pytest.raises(LoginRejectedError):
        rig.service.apply(edit, password=None, remote_addr=LOCAL)
    rig.service.apply(edit, password=GOOD, remote_addr=LOCAL)
    assert rig.heartbeat.identity.facts.name == "小蓝"


def test_wrong_passwords_for_the_baseline_count_toward_the_login_limit(tmp_path: Path) -> None:
    rig = Rig(tmp_path, max_failures=3)
    try:
        edit = IdentityEdit(text={IMMUTABLE: "新底线。"})
        for _ in range(3):
            with pytest.raises(LoginRejectedError):
                rig.service.apply(edit, password="wrong password", remote_addr="192.168.1.20")
        with pytest.raises(LoginLockedError):
            rig.service.apply(edit, password=GOOD, remote_addr="192.168.1.20")
        assert rig.file_text() == ORIGINAL
    finally:
        rig.close()


def test_a_lock_toggle_changes_the_identity_without_a_timeline_layer(rig: Rig) -> None:
    rig.service.apply(
        IdentityEdit(locks=PersonaLocks(mutable=True)), password=None, remote_addr=LOCAL
    )

    layers, _version, _via = rig.heartbeat.applied[0]
    assert layers == ()
    assert rig.heartbeat.identity.persona.locks.mutable is True
    assert rig.service.view()["locks"]["mutable"] is True


def test_a_refused_edit_leaves_the_file_and_the_heartbeat_alone(rig: Rig) -> None:
    with pytest.raises(IdentityEditError):
        rig.service.apply(IdentityEdit(text={IMMUTABLE: "   "}), password=GOOD, remote_addr=LOCAL)
    assert rig.file_text() == ORIGINAL
    assert rig.heartbeat.applied == []


# ---------- 回退 ----------


def make_versions(rig: Rig, count: int) -> None:
    for number in range(count):
        rig.service.apply(
            IdentityEdit(text={STYLE: f"第{number}版。"}), password=None, remote_addr=LOCAL
        )


def test_the_view_lists_only_the_latest_few_versions(rig: Rig) -> None:
    make_versions(rig, 5)  # 加上初始版本一共 6 个
    history = rig.service.view()["history"]["expression_style"]
    assert [v["content"] for v in history] == ["第2版。", "第3版。", "第4版。"]
    assert len(history) == ROLLBACK_CHOICES


def test_rolling_back_saves_a_new_version_and_keeps_the_history(rig: Rig) -> None:
    make_versions(rig, 3)
    target = rig.service.view()["history"]["expression_style"][0]  # 最近 3 个里最早的
    before = len(rig.history.versions(STYLE, limit=100))

    result = rig.service.roll_back(STYLE, target["id"], remote_addr=LOCAL)

    after = rig.history.versions(STYLE, limit=100)
    assert len(after) == before + 1  # 多了一个新版本，旧的一个都没少
    assert after[-1].content == target["content"]
    assert after[-1].source is VersionSource.USER_FILE
    assert result.changed_layers == (STYLE,)
    assert rig.heartbeat.identity.persona.expression_style.strip() == target["content"]
    assert target["content"] in rig.file_text()  # 同样写回文件


def test_rolling_back_self_change_to_file_text_updates_heartbeat(rig: Rig) -> None:
    original = rig.history.versions(STYLE, limit=1)[0]
    rig.history.record_self_change(STYLE, "句子短，多关心对方。")
    effective = rig.history.sync_from_file(Identity.model_validate(tomllib.loads(ORIGINAL)))
    rig.heartbeat.identity = effective
    result = rig.service.roll_back(STYLE, original.id, remote_addr=LOCAL)
    assert result.changed_layers == (STYLE,)
    assert rig.heartbeat.identity.persona.expression_style == original.content
    assert rig.heartbeat.applied[-1] == ((STYLE,), result.up_to_version_id, "web")
    version = rig.history.get(result.up_to_version_id)
    assert version is not None and version.source is VersionSource.USER_FILE


def test_rolling_back_to_an_older_version_than_the_latest_few_is_refused(rig: Rig) -> None:
    make_versions(rig, 5)
    oldest = rig.history.versions(STYLE, limit=100)[0]  # 初始版本，已经不在最近 3 个里
    with pytest.raises(IdentityEditError, match="最近的几个版本"):
        rig.service.roll_back(STYLE, oldest.id, remote_addr=LOCAL)


def test_rolling_back_to_a_version_of_another_layer_is_refused(rig: Rig) -> None:
    make_versions(rig, 1)
    other = rig.history.versions(IdentityLayer.MUTABLE, limit=1)[0]
    with pytest.raises(IdentityEditError):
        rig.service.roll_back(STYLE, other.id, remote_addr=LOCAL)


@pytest.mark.parametrize("layer", [IdentityLayer.FACTS, IdentityLayer.IMMUTABLE])
def test_the_baseline_layers_cannot_be_rolled_back_here(rig: Rig, layer: IdentityLayer) -> None:
    version = rig.history.versions(layer, limit=1)[0]
    with pytest.raises(IdentityEditError, match="不能在这里回退"):
        rig.service.roll_back(layer, version.id, remote_addr=LOCAL)
    assert rig.heartbeat.applied == []


def test_rolling_back_to_the_current_content_adds_no_version(rig: Rig) -> None:
    current = rig.history.versions(STYLE, limit=1)[0]
    result = rig.service.roll_back(STYLE, current.id, remote_addr=LOCAL)
    assert result.changed_layers == ()
    assert len(rig.history.versions(STYLE, limit=100)) == 1
