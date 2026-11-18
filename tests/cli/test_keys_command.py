"""keys 命令的测试：全部用编造的假密钥，数据根目录用临时文件夹。"""

from collections.abc import Callable
from pathlib import Path

import pytest

from aureus.cli.main import main
from aureus.common.paths import AureusPaths
from aureus.common.vault import SecretVault

NAME = "AUREUS_KEY_FAKE"
OTHER_NAME = "AUREUS_KEY_OTHER"
SECRET = "sk-fake-cli-0001"
OTHER_SECRET = "sk-fake-cli-0002"

Reader = Callable[[str], str]
Capture = pytest.CaptureFixture[str]


def never_prompted(prompt: str) -> str:
    raise AssertionError("这个命令不应该提示输入密钥")


def typing_in(secret: str) -> Reader:
    def reader(prompt: str) -> str:
        return secret

    return reader


def run(home: Path, *arguments: str, read_secret: Reader = never_prompted) -> int:
    return main(["--home", str(home), *arguments], read_secret=read_secret)


def open_vault(home: Path) -> SecretVault:
    paths = AureusPaths(home)
    return SecretVault(paths.vault_file, paths.master_key_file)


def write_dotenv(home: Path, *lines: str) -> Path:
    path = AureusPaths(home).env_file
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_set_stores_the_typed_secret_and_never_prints_it(tmp_path: Path, capsys: Capture) -> None:
    assert run(tmp_path, "keys", "set", NAME, read_secret=typing_in(SECRET)) == 0
    output = capsys.readouterr()
    assert NAME in output.out
    assert SECRET not in output.out + output.err
    assert open_vault(tmp_path).load() == {NAME: SECRET}


def test_set_again_updates_the_value(tmp_path: Path, capsys: Capture) -> None:
    run(tmp_path, "keys", "set", NAME, read_secret=typing_in(SECRET))
    capsys.readouterr()
    assert run(tmp_path, "keys", "set", NAME, read_secret=typing_in(OTHER_SECRET)) == 0
    assert "已更新" in capsys.readouterr().out
    assert open_vault(tmp_path).load() == {NAME: OTHER_SECRET}


def test_blank_input_changes_nothing(tmp_path: Path, capsys: Capture) -> None:
    assert run(tmp_path, "keys", "set", NAME, read_secret=typing_in("   ")) == 1
    assert "没有输入密钥" in capsys.readouterr().err
    paths = AureusPaths(tmp_path)
    assert not paths.vault_file.exists()
    assert not paths.master_key_file.exists()


def test_cancelling_the_prompt_changes_nothing(tmp_path: Path, capsys: Capture) -> None:
    def cancel(prompt: str) -> str:
        raise KeyboardInterrupt

    assert run(tmp_path, "keys", "set", NAME, read_secret=cancel) == 1
    assert "已取消" in capsys.readouterr().err
    assert not AureusPaths(tmp_path).vault_file.exists()


def test_bad_key_name_is_refused_without_echoing_it(tmp_path: Path, capsys: Capture) -> None:
    pasted = "sk-pasted-real-secret"
    with pytest.raises(SystemExit) as info:
        run(tmp_path, "keys", "set", pasted)
    assert info.value.code == 2
    assert pasted not in capsys.readouterr().err


def test_list_shows_names_only(tmp_path: Path, capsys: Capture) -> None:
    vault = open_vault(tmp_path)
    vault.set(NAME, SECRET)
    vault.set(OTHER_NAME, OTHER_SECRET)
    assert run(tmp_path, "keys", "list") == 0
    output = capsys.readouterr().out
    assert NAME in output
    assert OTHER_NAME in output
    assert SECRET not in output
    assert OTHER_SECRET not in output


def test_list_on_a_fresh_home_says_so_and_creates_nothing(tmp_path: Path, capsys: Capture) -> None:
    assert run(tmp_path, "keys", "list") == 0
    assert "还没有密钥" in capsys.readouterr().out
    assert not AureusPaths(tmp_path).vault_file.exists()


def test_remove_deletes_one_entry(tmp_path: Path, capsys: Capture) -> None:
    vault = open_vault(tmp_path)
    vault.set(NAME, SECRET)
    vault.set(OTHER_NAME, OTHER_SECRET)
    assert run(tmp_path, "keys", "remove", NAME) == 0
    assert "已删除" in capsys.readouterr().out
    assert vault.names() == [OTHER_NAME]


def test_remove_of_an_absent_name_changes_nothing(tmp_path: Path, capsys: Capture) -> None:
    vault = open_vault(tmp_path)
    vault.set(OTHER_NAME, OTHER_SECRET)
    assert run(tmp_path, "keys", "remove", NAME) == 0
    assert "没有" in capsys.readouterr().out
    assert vault.load() == {OTHER_NAME: OTHER_SECRET}


def test_migrate_moves_prefixed_keys_and_leaves_dotenv_alone(
    tmp_path: Path, capsys: Capture
) -> None:
    env_file = write_dotenv(
        tmp_path, f"{NAME}={SECRET}", "OTHER_SETTING=plain", "AUREUS_KEY_EMPTY="
    )
    before = env_file.read_bytes()
    assert run(tmp_path, "keys", "migrate") == 0
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "核对一致" in output.out
    assert open_vault(tmp_path).load() == {NAME: SECRET}
    assert env_file.read_bytes() == before


def test_migrate_does_not_overwrite_and_is_repeatable(tmp_path: Path, capsys: Capture) -> None:
    vault = open_vault(tmp_path)
    vault.set(NAME, OTHER_SECRET)
    write_dotenv(tmp_path, f"{NAME}={SECRET}", f"{OTHER_NAME}={OTHER_SECRET}")
    assert run(tmp_path, "keys", "migrate") == 0
    assert "不覆盖" in capsys.readouterr().out
    expected = {NAME: OTHER_SECRET, OTHER_NAME: OTHER_SECRET}
    assert vault.load() == expected
    assert run(tmp_path, "keys", "migrate") == 0
    assert "已搬进密钥库：" not in capsys.readouterr().out
    assert vault.load() == expected


def test_migrate_without_dotenv_says_so(tmp_path: Path, capsys: Capture) -> None:
    assert run(tmp_path, "keys", "migrate") == 0
    assert "没有找到" in capsys.readouterr().out
    assert not AureusPaths(tmp_path).vault_file.exists()


def test_migrate_with_nothing_to_move(tmp_path: Path, capsys: Capture) -> None:
    write_dotenv(tmp_path, "OTHER_SETTING=plain", "AUREUS_KEY_EMPTY=")
    assert run(tmp_path, "keys", "migrate") == 0
    assert "没有需要搬的密钥" in capsys.readouterr().out
    assert not AureusPaths(tmp_path).vault_file.exists()


def test_migrate_skips_badly_named_entries(tmp_path: Path, capsys: Capture) -> None:
    write_dotenv(tmp_path, f"AUREUS_KEY_lower={SECRET}", f"{NAME}={OTHER_SECRET}")
    assert run(tmp_path, "keys", "migrate") == 0
    output = capsys.readouterr()
    assert "AUREUS_KEY_lower" in output.out
    assert SECRET not in output.out + output.err
    assert open_vault(tmp_path).load() == {NAME: OTHER_SECRET}


def test_unopenable_vault_stops_before_asking_for_a_secret(tmp_path: Path, capsys: Capture) -> None:
    run(tmp_path, "keys", "set", NAME, read_secret=typing_in(SECRET))
    capsys.readouterr()
    AureusPaths(tmp_path).master_key_file.unlink()
    write_dotenv(tmp_path, f"{OTHER_NAME}={OTHER_SECRET}")
    # set 用默认的 never_prompted：如果它在库打不开时还去提示输入，测试会当场失败。
    for arguments in (["set", OTHER_NAME], ["list"], ["remove", NAME], ["migrate"]):
        assert run(tmp_path, "keys", *arguments) == 1
        assert "主密钥" in capsys.readouterr().err


def test_relative_home_is_resolved_against_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Capture
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["--home", "relative_home", "keys", "list"]) == 0
    assert str((tmp_path / "relative_home").resolve()) in capsys.readouterr().out


def test_home_comes_from_the_environment_when_not_given(tmp_path: Path, capsys: Capture) -> None:
    assert main(["keys", "list"], environ={"AUREUS_HOME": str(tmp_path)}) == 0
    assert str(tmp_path) in capsys.readouterr().out


def test_relative_home_in_the_environment_is_refused(capsys: Capture) -> None:
    assert main(["keys", "list"], environ={"AUREUS_HOME": "relative"}) == 1
    assert "绝对路径" in capsys.readouterr().err
