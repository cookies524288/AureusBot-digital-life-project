from pathlib import Path

import pytest
from loguru import logger

from aureus.common.vault import SecretVault, VaultError
from aureus.organs.api_keys import KeyResolver, warn_about_plaintext_keys

# 全部是编造的假密钥，不是真的。
NAME = "AUREUS_KEY_FAKE"
VAULT_SECRET = "vault-secret-0001"
ENV_SECRET = "env-secret-0002"
DOTENV_SECRET = "dotenv-secret-0003"
UNPREFIXED_VAULT_SECRET = "vault-unprefixed-0004"


@pytest.fixture
def vault(tmp_path: Path) -> SecretVault:
    return SecretVault(tmp_path / "secrets.vault", tmp_path / "secrets.key")


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def read(resolver: KeyResolver, name: str) -> str | None:
    secret = resolver.get(name)
    return None if secret is None else secret.get_secret_value()


def test_environment_beats_vault_and_vault_beats_dotenv(tmp_path: Path, vault: SecretVault) -> None:
    vault.set(NAME, VAULT_SECRET)
    env_file = write_env(tmp_path, f"{NAME}={DOTENV_SECRET}\n")
    assert read(KeyResolver(env_file, {NAME: ENV_SECRET}, vault), NAME) == ENV_SECRET
    assert read(KeyResolver(env_file, {}, vault), NAME) == VAULT_SECRET
    vault.remove(NAME)
    assert read(KeyResolver(env_file, {}, vault), NAME) == DOTENV_SECRET


def test_blank_environment_value_falls_through_to_the_vault(
    tmp_path: Path, vault: SecretVault
) -> None:
    vault.set(NAME, VAULT_SECRET)
    resolver = KeyResolver(tmp_path / "absent.env", {NAME: "   "}, vault)
    assert read(resolver, NAME) == VAULT_SECRET


def test_dotenv_still_works_without_a_vault(tmp_path: Path) -> None:
    resolver = KeyResolver(write_env(tmp_path, f"{NAME}={DOTENV_SECRET}\n"), {})
    assert read(resolver, NAME) == DOTENV_SECRET


def test_missing_everywhere_means_none(tmp_path: Path, vault: SecretVault) -> None:
    assert read(KeyResolver(tmp_path / "absent.env", {}, vault), NAME) is None


def test_unopenable_vault_stops_startup_instead_of_falling_back_to_dotenv(
    tmp_path: Path, vault: SecretVault
) -> None:
    vault.set(NAME, VAULT_SECRET)
    (tmp_path / "secrets.key").unlink()
    env_file = write_env(tmp_path, f"{NAME}={DOTENV_SECRET}\n")
    with pytest.raises(VaultError):
        KeyResolver(env_file, {}, vault)


def test_all_secret_values_cover_every_source_and_the_master_key(
    tmp_path: Path, vault: SecretVault
) -> None:
    vault.set(NAME, VAULT_SECRET)
    vault.set("not_prefixed", UNPREFIXED_VAULT_SECRET)
    env_file = write_env(
        tmp_path,
        f"AUREUS_KEY_DOTENV={DOTENV_SECRET}\nOTHER_SETTING=plain\nAUREUS_KEY_EMPTY=\n",
    )
    environ = {"AUREUS_KEY_ENV": ENV_SECRET, "PATH": "/usr/bin", "AUREUS_KEY_BLANK": "   "}
    resolver = KeyResolver(env_file, environ, vault)
    master_key = vault.master_key()
    assert master_key is not None
    expected = {
        VAULT_SECRET,
        UNPREFIXED_VAULT_SECRET,
        ENV_SECRET,
        DOTENV_SECRET,
        master_key.decode("ascii"),
    }
    assert resolver.all_secret_values() == sorted(expected)


def test_all_secret_values_has_no_duplicates(tmp_path: Path) -> None:
    env_file = write_env(tmp_path, f"AUREUS_KEY_B={ENV_SECRET}\n")
    resolver = KeyResolver(env_file, {"AUREUS_KEY_A": ENV_SECRET})
    assert resolver.all_secret_values() == [ENV_SECRET]


def test_master_key_is_hidden_even_when_the_vault_file_is_gone(
    tmp_path: Path, vault: SecretVault
) -> None:
    vault.set(NAME, VAULT_SECRET)
    (tmp_path / "secrets.vault").unlink()
    master_key = vault.master_key()
    assert master_key is not None
    resolver = KeyResolver(tmp_path / "absent.env", {}, vault)
    assert resolver.all_secret_values() == [master_key.decode("ascii")]


def test_plaintext_key_names_lists_only_filled_prefixed_names(tmp_path: Path) -> None:
    env_file = write_env(
        tmp_path,
        f"AUREUS_KEY_B={DOTENV_SECRET}\nAUREUS_KEY_A=x\nAUREUS_KEY_C=\nOTHER=y\n",
    )
    resolver = KeyResolver(env_file, {"AUREUS_KEY_FROM_ENV": ENV_SECRET})
    assert resolver.plaintext_key_names() == ["AUREUS_KEY_A", "AUREUS_KEY_B"]


def capture_warnings(resolver: KeyResolver) -> str:
    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(str(message)), level="WARNING", format="{message}"
    )
    try:
        warn_about_plaintext_keys(resolver)
    finally:
        logger.remove(sink_id)
    return "".join(messages)


def test_warning_names_the_keys_but_never_their_values(tmp_path: Path) -> None:
    env_file = write_env(tmp_path, f"AUREUS_KEY_A={DOTENV_SECRET}\n")
    text = capture_warnings(KeyResolver(env_file, {}))
    assert "AUREUS_KEY_A" in text
    assert "keys migrate" in text
    assert DOTENV_SECRET not in text


def test_no_warning_when_dotenv_has_no_keys(tmp_path: Path, vault: SecretVault) -> None:
    vault.set(NAME, VAULT_SECRET)
    resolver = KeyResolver(tmp_path / "absent.env", {NAME: ENV_SECRET}, vault)
    assert capture_warnings(resolver) == ""
