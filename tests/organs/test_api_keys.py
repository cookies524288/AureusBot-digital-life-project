"""按环境变量名取密钥的测试。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from aureus.common.vault import SecretVault
from aureus.organs.api_keys import KeyResolver
from aureus.organs.model_config import ProviderConfig


def read(resolver: KeyResolver, name: str) -> str | None:
    secret = resolver.get(name)
    return None if secret is None else secret.get_secret_value()


def write_env(tmp_path: Path, text: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    return env_file


def test_environment_wins_over_env_file(tmp_path: Path) -> None:
    env_file = write_env(tmp_path, "AUREUS_KEY_A=from-file\n")
    resolver = KeyResolver(env_file, {"AUREUS_KEY_A": "from-env"})
    assert read(resolver, "AUREUS_KEY_A") == "from-env"


def test_env_file_is_used_when_environment_lacks_the_name(tmp_path: Path) -> None:
    resolver = KeyResolver(write_env(tmp_path, "AUREUS_KEY_A=from-file\n"), {})
    assert read(resolver, "AUREUS_KEY_A") == "from-file"


def test_blank_environment_value_falls_back_to_env_file(tmp_path: Path) -> None:
    resolver = KeyResolver(write_env(tmp_path, "AUREUS_KEY_A=from-file\n"), {"AUREUS_KEY_A": "   "})
    assert read(resolver, "AUREUS_KEY_A") == "from-file"


def test_missing_or_blank_keys_are_none(tmp_path: Path) -> None:
    # .env 文件不存在也不报错；写了变量名但没填值，等于没有。
    assert read(KeyResolver(tmp_path / "absent.env", {}), "AUREUS_KEY_A") is None
    resolver = KeyResolver(write_env(tmp_path, "AUREUS_KEY_A=\nAUREUS_KEY_B\n"), {})
    assert read(resolver, "AUREUS_KEY_A") is None
    assert read(resolver, "AUREUS_KEY_B") is None


def test_surrounding_whitespace_is_stripped(tmp_path: Path) -> None:
    resolver = KeyResolver(tmp_path / "absent.env", {"AUREUS_KEY_A": "  padded  "})
    assert read(resolver, "AUREUS_KEY_A") == "padded"


def test_secret_is_masked_when_printed(tmp_path: Path) -> None:
    resolver = KeyResolver(tmp_path / "absent.env", {"AUREUS_KEY_A": "sk-visible-never"})
    assert "sk-visible-never" not in repr(resolver.get("AUREUS_KEY_A"))


def test_secret_values_lists_only_filled_keys(tmp_path: Path) -> None:
    resolver = KeyResolver(tmp_path / "absent.env", {"AUREUS_KEY_A": "one"})
    assert resolver.secret_values(["AUREUS_KEY_A", "AUREUS_KEY_B"]) == ["one"]


def test_explicit_vault_reference_uses_only_the_unlocked_api_key_snapshot(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.vault", tmp_path / "secrets.key")
    entry = vault.create("PATH", "api_key", "fake-vault-api")
    password = vault.create("网站密码", "password", " fake-password ")
    resolver = KeyResolver(tmp_path / "absent.env", {"PATH": "ordinary-setting"}, vault)
    vault.remove_entry(entry.id)
    # 模型取值不读文件；网页通过 replace_vault 显式切换快照。
    secret = resolver.get_vault(entry.id)
    assert secret is not None and secret.get_secret_value() == "fake-vault-api"
    assert resolver.get_vault(password.id) is None
    assert " fake-password " in resolver.all_secret_values()
    resolver.replace_vault(vault.load_entries(), vault.master_key())
    assert resolver.get_vault(entry.id) is None
    assert resolver.get("PATH") is None


def test_provider_rejects_two_key_sources_and_invalid_references() -> None:
    with pytest.raises(ValidationError, match="只能选择一个"):
        ProviderConfig(
            name="p", protocol="openai", api_key_env="AUREUS_KEY_A", api_key_ref="a" * 32
        )
    with pytest.raises(ValidationError):
        ProviderConfig(name="p", protocol="openai", api_key_ref="not-a-reference")
