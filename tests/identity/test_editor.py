"""网页改设定、写回 identity.toml 的测试（路线图 1.10，D63 第 1、2 条）。"""

import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.identity import (
    Facts,
    Identity,
    IdentityEdit,
    IdentityEditError,
    IdentityEditor,
    IdentityHistory,
    IdentityLayer,
    IdentityValidationError,
    PersonaLocks,
    User,
    UserAccount,
    VersionSource,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

ORIGINAL = '''# 我自己写的注释：别弄丢
[facts]
name = "小绿"
alias_names = []
gender = ""
birthday = "2008-01-01"

[user]
name = "阿明"  # 行尾注释
accounts = [
    { platform = "qq", id = "10000" },
]

[persona]
immutable = """
温和。
"""
mutable = """
"""
# 表达风格的注释
expression_style = """
句子短。
"""

[persona.locks]
mutable = false
behavior_style = false
expression_style = false
'''


@pytest.fixture
def identity_file(tmp_path: Path) -> Path:
    path = tmp_path / "identity.toml"
    path.write_bytes(ORIGINAL.encode("utf-8"))
    return path


@pytest.fixture
def history(tmp_path: Path) -> Iterator[IdentityHistory]:
    opened = IdentityHistory(tmp_path / "aureus.db", ManualClock(NOW))
    opened.open()
    yield opened
    opened.close()


@pytest.fixture
def editor(identity_file: Path, history: IdentityHistory) -> IdentityEditor:
    # 先像启动那样和文件对一遍，生成初始版本。
    history.sync_from_file(Identity.model_validate(tomllib.loads(ORIGINAL)))
    return IdentityEditor(identity_file, history)


def read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_bytes().decode("utf-8"))


def test_editing_a_style_changes_only_that_key_and_keeps_comments(
    editor: IdentityEditor, identity_file: Path
) -> None:
    result = editor.apply(IdentityEdit(text={IdentityLayer.EXPRESSION_STYLE: "说话要慢一点。"}))

    text = identity_file.read_bytes().decode("utf-8")
    assert "# 我自己写的注释：别弄丢" in text
    assert "# 行尾注释" in text
    assert "# 表达风格的注释" in text
    data = read_toml(identity_file)
    assert data["persona"]["expression_style"].strip() == "说话要慢一点。"
    assert data["persona"]["immutable"].strip() == "温和。"  # 别的层没动
    assert data["user"]["accounts"] == [{"platform": "qq", "id": "10000"}]
    assert result.changed_layers == (IdentityLayer.EXPRESSION_STYLE,)
    assert result.identity.persona.expression_style.strip() == "说话要慢一点。"


def test_the_change_is_recorded_as_a_user_version(
    editor: IdentityEditor, history: IdentityHistory
) -> None:
    result = editor.apply(IdentityEdit(text={IdentityLayer.BEHAVIOR_STYLE: "少说话。"}))

    latest = history.versions(IdentityLayer.BEHAVIOR_STYLE, limit=1)[0]
    assert latest.source is VersionSource.USER_FILE  # 用户决定：不新增 user_web 来源
    assert latest.content == "少说话。"
    assert latest.id == result.up_to_version_id


def test_a_restart_does_not_count_the_web_edit_as_a_new_user_change(
    editor: IdentityEditor, history: IdentityHistory, identity_file: Path
) -> None:
    result = editor.apply(IdentityEdit(text={IdentityLayer.MUTABLE: "喜欢下雨天。"}))

    history.sync_from_file(Identity.model_validate(read_toml(identity_file)))  # 模拟重启
    assert history.user_changes_after(result.up_to_version_id) == []


def test_saving_the_same_content_adds_no_version(
    editor: IdentityEditor, history: IdentityHistory
) -> None:
    before = len(history.versions(None, limit=100))
    result = editor.apply(IdentityEdit(text={IdentityLayer.EXPRESSION_STYLE: "句子短。\n\n"}))

    assert result.changed_layers == ()
    assert len(history.versions(None, limit=100)) == before


def test_editing_the_immutable_persona_needs_the_password() -> None:
    assert IdentityEdit(text={IdentityLayer.IMMUTABLE: "新底线。"}).requires_password()
    facts = Facts(name="小蓝", birthday="2008-01-01")
    assert IdentityEdit(facts=facts).requires_password()
    assert IdentityEdit(user=User(name="阿明")).requires_password()


def test_the_three_free_layers_and_locks_do_not_need_the_password() -> None:
    for layer in (
        IdentityLayer.MUTABLE,
        IdentityLayer.BEHAVIOR_STYLE,
        IdentityLayer.EXPRESSION_STYLE,
    ):
        assert not IdentityEdit(text={layer: "x"}).requires_password()
    assert not IdentityEdit(locks=PersonaLocks(mutable=True)).requires_password()


def test_editing_the_immutable_persona_is_recorded(
    editor: IdentityEditor, history: IdentityHistory
) -> None:
    result = editor.apply(IdentityEdit(text={IdentityLayer.IMMUTABLE: "新的底线。"}))

    assert result.changed_layers == (IdentityLayer.IMMUTABLE,)
    assert history.versions(IdentityLayer.IMMUTABLE, limit=1)[0].content == "新的底线。"


def test_editing_facts_and_user_accounts(editor: IdentityEditor, identity_file: Path) -> None:
    result = editor.apply(
        IdentityEdit(
            facts=Facts(name="小蓝", alias_names=["蓝蓝"], gender="女", birthday="2009-02-03"),
            user=User(
                name="阿强",
                accounts=[
                    UserAccount(platform="qq", id="10000"),
                    UserAccount(platform="wechat", id="w1"),
                ],
            ),
        )
    )

    data = read_toml(identity_file)
    assert data["facts"] == {
        "name": "小蓝",
        "alias_names": ["蓝蓝"],
        "gender": "女",
        "birthday": "2009-02-03",
    }
    assert data["user"]["name"] == "阿强"
    assert [a["platform"] for a in data["user"]["accounts"]] == ["qq", "wechat"]
    assert result.changed_layers == (IdentityLayer.FACTS,)  # 事实和用户记在同一层


def test_toggling_a_lock_writes_the_file_but_adds_no_version(
    editor: IdentityEditor, history: IdentityHistory, identity_file: Path
) -> None:
    before = len(history.versions(None, limit=100))
    result = editor.apply(IdentityEdit(locks=PersonaLocks(expression_style=True)))

    assert read_toml(identity_file)["persona"]["locks"]["expression_style"] is True
    assert result.identity.persona.locks.expression_style is True
    assert result.changed_layers == ()
    assert len(history.versions(None, limit=100)) == before


def test_a_lock_set_on_the_web_stops_it_from_changing_that_layer(
    editor: IdentityEditor, history: IdentityHistory
) -> None:
    editor.apply(IdentityEdit(locks=PersonaLocks(mutable=True)))

    from aureus.identity import IdentityHistoryError

    with pytest.raises(IdentityHistoryError):
        history.record_self_change(IdentityLayer.MUTABLE, "它想自己改")


def test_the_user_can_still_edit_a_locked_layer(editor: IdentityEditor) -> None:
    editor.apply(IdentityEdit(locks=PersonaLocks(mutable=True)))
    result = editor.apply(IdentityEdit(text={IdentityLayer.MUTABLE: "用户自己改。"}))
    assert result.changed_layers == (IdentityLayer.MUTABLE,)


def test_an_empty_immutable_persona_is_refused_and_the_file_is_untouched(
    editor: IdentityEditor, identity_file: Path
) -> None:
    with pytest.raises(IdentityEditError, match="不变人格不能是空的"):
        editor.apply(IdentityEdit(text={IdentityLayer.IMMUTABLE: "   \n"}))
    assert identity_file.read_bytes().decode("utf-8") == ORIGINAL


def test_values_that_cannot_be_written_as_toml_are_refused_and_the_file_is_untouched(
    editor: IdentityEditor, identity_file: Path
) -> None:
    bad = Facts.model_construct(name="小绿", alias_names=[], gender="", birthday=None)
    with pytest.raises(IdentityEditError, match="写不进 TOML"):
        editor.apply(IdentityEdit(facts=bad))
    assert identity_file.read_bytes().decode("utf-8") == ORIGINAL


def test_a_text_layer_that_is_not_text_is_refused(editor: IdentityEditor) -> None:
    with pytest.raises(IdentityEditError):
        editor.apply(IdentityEdit(text={IdentityLayer.FACTS: "x"}))


def test_a_broken_file_is_not_touched(identity_file: Path, history: IdentityHistory) -> None:
    identity_file.write_bytes(b"[persona\nbroken")
    editor = IdentityEditor(identity_file, history)
    with pytest.raises(IdentityEditError, match="不是合法的 TOML"):
        editor.apply(IdentityEdit(text={IdentityLayer.MUTABLE: "x"}))
    assert identity_file.read_bytes() == b"[persona\nbroken"


def test_a_missing_file_is_reported(tmp_path: Path, history: IdentityHistory) -> None:
    editor = IdentityEditor(tmp_path / "nope.toml", history)
    with pytest.raises(IdentityEditError):
        editor.apply(IdentityEdit(text={IdentityLayer.MUTABLE: "x"}))


def test_a_file_without_the_persona_section_is_refused(
    tmp_path: Path, history: IdentityHistory
) -> None:
    path = tmp_path / "identity.toml"
    path.write_bytes(b'[facts]\nname = "x"\n')
    with pytest.raises(IdentityEditError, match=r"\[persona\]"):
        IdentityEditor(path, history).apply(IdentityEdit(text={IdentityLayer.MUTABLE: "x"}))


def test_no_temp_files_are_left_behind(editor: IdentityEditor, identity_file: Path) -> None:
    editor.apply(IdentityEdit(text={IdentityLayer.MUTABLE: "a"}))
    with pytest.raises(IdentityEditError):
        editor.apply(IdentityEdit(text={IdentityLayer.IMMUTABLE: ""}))
    assert [p.name for p in identity_file.parent.glob(".identity-*")] == []


def test_content_with_quotes_and_triple_quotes_round_trips(
    editor: IdentityEditor, identity_file: Path
) -> None:
    tricky = '他说"你好"，还说"""三个引号"""，路径 C:\\temp，换行\n第二行'
    editor.apply(IdentityEdit(text={IdentityLayer.MUTABLE: tricky}))
    assert read_toml(identity_file)["persona"]["mutable"].strip() == tricky


def test_a_result_that_breaks_the_schema_is_refused_and_the_file_is_untouched(
    tmp_path: Path, history: IdentityHistory
) -> None:
    # 文件里本来就缺必填的 birthday：改别的层也不能把一个不合规范的文件写出去。
    broken = ORIGINAL.replace('birthday = "2008-01-01"\n', "")
    path = tmp_path / "broken.toml"
    path.write_bytes(broken.encode("utf-8"))
    with pytest.raises(IdentityValidationError):
        IdentityEditor(path, history).apply(IdentityEdit(text={IdentityLayer.MUTABLE: "x"}))
    assert path.read_bytes().decode("utf-8") == broken
