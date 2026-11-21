"""通用密钥记录：类型、改名、旧格式升级和不丢失原文。"""

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from aureus.common.vault import SecretVault, VaultError, load_master_key


@pytest.fixture
def vault(tmp_path: Path) -> SecretVault:
    return SecretVault(tmp_path / "secrets.vault", tmp_path / "secrets.key")


def test_typed_entries_preserve_password_and_other_whitespace(vault: SecretVault) -> None:
    for kind in ("password", "other"):
        entry = vault.create(kind, kind, "  fake-secret\n  ")
        assert entry.value.get_secret_value() == "  fake-secret\n  "
        assert "fake-secret" not in repr(entry)
    api = vault.create("模型凭据", "api_key", "  fake-api-key\n")
    assert api.value.get_secret_value() == "fake-api-key"
    assert len(vault.load_entries()) == 3


def test_rename_keeps_identity_and_duplicate_names_cannot_overwrite(vault: SecretVault) -> None:
    first = vault.create("原来的名称", "api_key", "fake-original")
    vault.create("其他名称", "password", "fake-password")
    assert vault.rename(first.id, "新的名称")
    renamed = next(item for item in vault.load_entries() if item.id == first.id)
    assert renamed.name == "新的名称"
    assert renamed.value == first.value
    with pytest.raises(VaultError, match="已存在"):
        vault.rename(first.id, "其他名称")
    with pytest.raises(VaultError, match="已存在"):
        vault.create("新的名称", "other", "fake-replacement")
    assert vault.remove_entry(first.id)
    assert not vault.remove_entry(first.id)


def test_legacy_alias_is_reserved_after_rename(vault: SecretVault) -> None:
    vault.set("AUREUS_KEY_OLD", "fake-original")
    entry = vault.load_entries()[0]
    vault.rename(entry.id, "显示名称")
    with pytest.raises(VaultError, match="已存在"):
        vault.create("AUREUS_KEY_OLD", "password", "fake-new")
    assert vault.load() == {"显示名称": "fake-original"}


def test_v1_read_is_nonmutating_and_first_edit_upgrades_encrypted_payload(
    vault: SecretVault, tmp_path: Path
) -> None:
    vault.set("AUREUS_KEY_OLD", "fake-legacy-key")
    key = load_master_key(tmp_path / "secrets.key")
    assert key is not None
    path = tmp_path / "secrets.vault"
    token = Fernet(key).encrypt(b'{"AUREUS_KEY_OLD":"fake-legacy-key"}').decode("ascii")
    path.write_text(json.dumps({"format_version": 1, "token": token}), encoding="utf-8")
    before = path.read_bytes()
    entry = vault.load_entries()[0]
    assert vault.load_entries()[0].id == entry.id
    assert entry.kind == "api_key"
    assert path.read_bytes() == before
    vault.rename(entry.id, "改过的名称")
    assert vault.load_entries()[0].id == entry.id
    assert vault.load_entries()[0].legacy_name == "AUREUS_KEY_OLD"
    assert json.loads(path.read_bytes())["format_version"] == 2
    assert b"fake-legacy-key" not in path.read_bytes()
    assert "改过的名称" not in path.read_text(encoding="utf-8")
