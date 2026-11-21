import json
import os
import threading
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from filelock import FileLock

from aureus.common import vault as vault_module
from aureus.common.vault import (
    KEY_FILE_FORMAT_VERSION,
    PROTECTION_NONE,
    VAULT_FORMAT_VERSION,
    SecretVault,
    VaultEntry,
    VaultError,
    generate_master_key,
    load_master_key,
)

# 全部是编造的假密钥，不是真的。
FAKE_NAME = "AUREUS_KEY_FAKE"
FAKE_SECRET = "sk-fake-secret-0001"
OTHER_NAME = "AUREUS_KEY_OTHER"


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    return tmp_path / "secrets.key"


@pytest.fixture
def vault_file(tmp_path: Path) -> Path:
    return tmp_path / "secrets.vault"


@pytest.fixture
def secret_vault(vault_file: Path, key_file: Path) -> SecretVault:
    return SecretVault(vault_file, key_file)


def write_key_file(path: Path, key: bytes) -> None:
    body = {
        "format_version": KEY_FILE_FORMAT_VERSION,
        "protection": PROTECTION_NONE,
        "key": key.decode("ascii"),
    }
    path.write_text(json.dumps(body), encoding="utf-8")


def test_no_files_means_empty_vault_and_nothing_is_created(
    secret_vault: SecretVault, tmp_path: Path
) -> None:
    assert secret_vault.load() == {}
    assert secret_vault.names() == []
    assert list(tmp_path.iterdir()) == []


def test_first_set_creates_master_key_and_vault(
    secret_vault: SecretVault, key_file: Path, vault_file: Path
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    assert key_file.exists()
    assert vault_file.exists()
    assert secret_vault.load() == {FAKE_NAME: FAKE_SECRET}


def test_values_are_stripped_and_blank_input_is_refused(secret_vault: SecretVault) -> None:
    secret_vault.set(FAKE_NAME, f"  {FAKE_SECRET}  ")
    assert secret_vault.load()[FAKE_NAME] == FAKE_SECRET
    with pytest.raises(ValueError):
        secret_vault.set(FAKE_NAME, "   ")
    with pytest.raises(ValueError):
        secret_vault.set("  ", FAKE_SECRET)
    assert secret_vault.load() == {FAKE_NAME: FAKE_SECRET}


def test_names_and_secrets_are_not_readable_in_the_vault_file(
    secret_vault: SecretVault, vault_file: Path
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    stored = vault_file.read_text(encoding="utf-8")
    assert FAKE_NAME not in stored
    assert FAKE_SECRET not in stored


def test_remove_reports_whether_the_name_existed(secret_vault: SecretVault) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    secret_vault.set(OTHER_NAME, "two")
    assert secret_vault.remove(FAKE_NAME) is True
    assert secret_vault.remove(FAKE_NAME) is False
    assert secret_vault.names() == [OTHER_NAME]


def test_vault_with_no_entries_is_valid(secret_vault: SecretVault, vault_file: Path) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    secret_vault.remove(FAKE_NAME)
    assert vault_file.exists()
    assert secret_vault.load() == {}


def test_wrong_master_key_fails_instead_of_reading_nothing(
    secret_vault: SecretVault, key_file: Path
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    write_key_file(key_file, generate_master_key())
    with pytest.raises(VaultError, match="解不开"):
        secret_vault.load()


def test_missing_master_key_fails_and_is_not_regenerated(
    secret_vault: SecretVault, key_file: Path
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    key_file.unlink()
    with pytest.raises(VaultError, match="找不到主密钥文件"):
        secret_vault.load()
    with pytest.raises(VaultError, match="找不到主密钥文件"):
        secret_vault.set(OTHER_NAME, "x")
    assert not key_file.exists()


BAD_KEY_FILE_CONTENTS = [
    "",
    "not json",
    "[]",
    '{"format_version": 1, "protection": "none"}',
    '{"format_version": 1, "protection": "none", "key": "too-short"}',
    '{"format_version": 1, "protection": "passphrase", "key": "x"}',
    '{"protection": "none", "key": "x"}',
]


@pytest.mark.parametrize("content", BAD_KEY_FILE_CONTENTS)
def test_unusable_master_key_file_is_refused(
    secret_vault: SecretVault, key_file: Path, content: str
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    key_file.write_text(content, encoding="utf-8")
    with pytest.raises(VaultError):
        secret_vault.load()
    with pytest.raises(VaultError):
        secret_vault.set(OTHER_NAME, "x")


def test_whitespace_around_the_master_key_is_tolerated(
    secret_vault: SecretVault, key_file: Path
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    body = json.loads(key_file.read_text(encoding="utf-8"))
    body["key"] = f" {body['key']} "
    key_file.write_text("\n  " + json.dumps(body) + "\n\n", encoding="utf-8")
    assert secret_vault.load() == {FAKE_NAME: FAKE_SECRET}


def test_changing_one_character_of_the_vault_is_detected(
    secret_vault: SecretVault, vault_file: Path
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    body = json.loads(vault_file.read_text(encoding="utf-8"))
    token: str = body["token"]
    middle = len(token) // 2
    body["token"] = token[:middle] + ("B" if token[middle] == "A" else "A") + token[middle + 1 :]
    vault_file.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(VaultError, match="解不开") as info:
        secret_vault.load()
    assert FAKE_SECRET not in str(info.value)


def test_newer_vault_format_is_refused(secret_vault: SecretVault, vault_file: Path) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    body = json.loads(vault_file.read_text(encoding="utf-8"))
    body["format_version"] = VAULT_FORMAT_VERSION + 1
    vault_file.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(VaultError, match="升级软件"):
        secret_vault.load()


def test_newer_master_key_format_is_refused(secret_vault: SecretVault, key_file: Path) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    body = json.loads(key_file.read_text(encoding="utf-8"))
    body["format_version"] = KEY_FILE_FORMAT_VERSION + 1
    key_file.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(VaultError, match="升级软件"):
        secret_vault.load()


BAD_VAULT_FILE_CONTENTS = [
    "",
    "[1]",
    "{}",
    '{"format_version": 1}',
    '{"format_version": 1, "token": "zzz"}',
]


@pytest.mark.parametrize("content", BAD_VAULT_FILE_CONTENTS)
def test_unusable_vault_file_is_refused(
    secret_vault: SecretVault, vault_file: Path, content: str
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    vault_file.write_text(content, encoding="utf-8")
    with pytest.raises(VaultError):
        secret_vault.load()


def test_decrypted_content_must_be_a_name_to_secret_table(
    secret_vault: SecretVault, key_file: Path, vault_file: Path
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    key = load_master_key(key_file)
    assert key is not None
    token = Fernet(key).encrypt(b'["not", "a", "table"]').decode("ascii")
    body = {"format_version": VAULT_FORMAT_VERSION, "token": token}
    vault_file.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(VaultError, match="内容格式不对"):
        secret_vault.load()


def test_master_key_without_vault_counts_as_no_vault(
    secret_vault: SecretVault, key_file: Path
) -> None:
    key = generate_master_key()
    write_key_file(key_file, key)
    assert secret_vault.load() == {}
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    assert load_master_key(key_file) == key


def test_load_master_key_returns_none_when_the_file_is_absent(key_file: Path) -> None:
    assert load_master_key(key_file) is None


def test_failed_write_leaves_the_old_vault_intact(
    secret_vault: SecretVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    before = sorted(path.name for path in tmp_path.iterdir())

    def broken_replace(source: object, destination: object) -> None:
        raise OSError("磁盘写满了（测试用）")

    monkeypatch.setattr(vault_module.os, "replace", broken_replace)
    with pytest.raises(OSError):
        secret_vault.set(OTHER_NAME, "x")
    monkeypatch.undo()
    assert secret_vault.load() == {FAKE_NAME: FAKE_SECRET}
    assert sorted(path.name for path in tmp_path.iterdir()) == before


@pytest.mark.skipif(os.name != "posix", reason="只有类 Unix 系统有这样的权限位")
def test_master_key_file_is_only_readable_by_its_owner(
    secret_vault: SecretVault, key_file: Path
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    assert key_file.stat().st_mode & 0o777 == 0o600


def test_concurrent_writes_do_not_lose_an_entry(
    secret_vault: SecretVault, vault_file: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 审查 H5：两个 keys set 同时「读—改—写」，没有锁时后写的会覆盖先写的。
    # 把读放慢，让两个线程一定会交错；有锁时两条都在。
    real_load = SecretVault.load_entries

    def slow_load(self: SecretVault) -> list[VaultEntry]:
        loaded = real_load(self)
        time.sleep(0.2)
        return loaded

    monkeypatch.setattr(SecretVault, "load_entries", slow_load)
    other = SecretVault(vault_file, key_file)  # 另一个进程里的 keys 命令会是另一个对象
    threads = [
        threading.Thread(target=secret_vault.set, args=(FAKE_NAME, FAKE_SECRET)),
        threading.Thread(target=other.set, args=(OTHER_NAME, "sk-fake-secret-0002")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.undo()

    assert secret_vault.names() == sorted([FAKE_NAME, OTHER_NAME])


def test_waiting_too_long_for_the_lock_fails_instead_of_writing(
    secret_vault: SecretVault, vault_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_vault.set(FAKE_NAME, FAKE_SECRET)
    monkeypatch.setattr(vault_module, "VAULT_LOCK_TIMEOUT_SECONDS", 0.1)
    held_elsewhere = FileLock(vault_file.with_name(vault_file.name + ".lock"))
    with held_elsewhere, pytest.raises(VaultError, match="另一个程序还在改密钥库"):
        SecretVault(vault_file, vault_file.with_name("secrets.key")).set(OTHER_NAME, "x")
    assert secret_vault.names() == [FAKE_NAME]
