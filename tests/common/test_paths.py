"""数据根目录与路径的测试。"""

from pathlib import Path

import pytest

from aureus.common import AureusPaths, HomePathError, home_is_default, resolve_home
from aureus.common.paths import HOME_ENV_VAR, PROJECT_HOME


def test_explicit_argument_wins_over_environment(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    environ = {HOME_ENV_VAR: str(tmp_path / "from-env")}
    assert resolve_home(explicit, environ) == explicit


def test_environment_variable_is_used_without_explicit_argument(tmp_path: Path) -> None:
    environ = {HOME_ENV_VAR: str(tmp_path / "from-env")}
    assert resolve_home(None, environ) == tmp_path / "from-env"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_values_fall_back_to_project_home(blank: str) -> None:
    home = resolve_home(blank, {HOME_ENV_VAR: blank})
    assert home == PROJECT_HOME


def test_no_home_uses_aureus_home_in_the_project_checkout() -> None:
    assert resolve_home(None, {}) == PROJECT_HOME


def test_home_directory_shortcut_is_expanded() -> None:
    home = resolve_home("~/aureus-test-home", {})
    assert home == Path.home() / "aureus-test-home"


def test_relative_path_is_rejected_and_origin_is_named() -> None:
    with pytest.raises(HomePathError, match="命令行参数"):
        resolve_home("relative/home", {})
    with pytest.raises(HomePathError, match=HOME_ENV_VAR):
        resolve_home(None, {HOME_ENV_VAR: "relative/home"})


def test_all_paths_are_derived_from_home(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    assert paths.runtime_config_file == tmp_path / "config" / "aureus.toml"
    assert paths.identity_file == tmp_path / "config" / "identity.toml"
    assert paths.env_file == tmp_path / ".env"
    assert paths.web_password_file == tmp_path / "config" / "web_password.argon2"
    assert paths.database_file == tmp_path / "data" / "aureus.db"
    assert paths.log_dir == tmp_path / "data" / "logs"
    everything = (
        paths.config_dir,
        paths.data_dir,
        paths.diary_dir,
        paths.memory_dir,
        paths.skills_dir,
        paths.snapshots_dir,
    )
    assert all(path.is_relative_to(tmp_path) for path in everything)


def test_relative_home_is_rejected() -> None:
    with pytest.raises(HomePathError, match="绝对路径"):
        AureusPaths(Path("relative"))


def test_ensure_directories_creates_folders_but_no_files(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    for folder in (paths.config_dir, paths.log_dir, paths.diary_dir, paths.memory_dir):
        assert folder.is_dir()
    assert paths.skills_dir.is_dir()
    assert paths.snapshots_dir.is_dir()
    assert not paths.database_file.exists()
    assert not paths.env_file.exists()
    assert not paths.runtime_config_file.exists()
    # 没有密码文件就是默认密码状态（D63），初始化时不能替用户建出一个。
    assert not paths.web_password_file.exists()


def test_ensure_directories_is_repeatable_and_keeps_existing_files(tmp_path: Path) -> None:
    paths = AureusPaths(tmp_path)
    paths.ensure_directories()
    marker = paths.log_dir / "keep.txt"
    marker.write_text("x", encoding="utf-8")
    paths.ensure_directories()
    assert marker.read_text(encoding="utf-8") == "x"


def test_ensure_directories_rejects_a_file_as_home(tmp_path: Path) -> None:
    not_a_folder = tmp_path / "not-a-folder"
    not_a_folder.write_text("x", encoding="utf-8")
    with pytest.raises(HomePathError, match="文件"):
        AureusPaths(not_a_folder).ensure_directories()


def test_home_is_default_only_without_argument_and_environment(tmp_path: Path) -> None:
    assert home_is_default(None, {})
    assert home_is_default("  ", {"AUREUS_HOME": " "})
    assert not home_is_default(str(tmp_path), {})
    assert not home_is_default(None, {"AUREUS_HOME": str(tmp_path)})
