"""按环境变量名取密钥的测试。"""

from pathlib import Path

from aureus.organs.api_keys import KeyResolver


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
