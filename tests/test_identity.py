"""身份子系统的测试。"""

import tempfile
import textwrap
from collections.abc import Generator
from pathlib import Path

import pytest

from aureus.common import AureusPaths
from aureus.identity import (
    Identity,
    IdentityFileNotFoundError,
    IdentityService,
    IdentityValidationError,
    SingleInstanceLockError,
)


@pytest.fixture
def temp_home() -> Generator[Path, None, None]:
    """创建临时数据根目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def paths(temp_home: Path) -> AureusPaths:
    """创建测试用的路径配置。"""
    return AureusPaths(home=temp_home)


@pytest.fixture
def valid_identity_content() -> str:
    """合法的 identity.toml 内容。"""
    return textwrap.dedent(
        """\
        [facts]
        name = "测试Bot"
        gender = "中性"
        birthday = "2024-01-01"

        [user]
        name = "测试用户"
        accounts = [
            { platform = "qq", id = "12345" },
        ]

        [persona]
        immutable = "你是一个测试用的 Bot。"
        mutable = ""
        behavior_style = "简短回应。"
        expression_style = "口语化。"

        [persona.locks]
        mutable = false
        behavior_style = false
        expression_style = false
        """
    )


def test_load_identity_success(paths: AureusPaths, valid_identity_content: str):
    """测试成功读取身份配置。"""
    paths.identity_file.parent.mkdir(parents=True, exist_ok=True)
    paths.identity_file.write_text(valid_identity_content, encoding="utf-8")

    service = IdentityService(paths)
    identity = service.load_identity()

    assert isinstance(identity, Identity)
    assert identity.facts.name == "测试Bot"
    assert identity.facts.gender == "中性"
    assert identity.facts.birthday == "2024-01-01"
    assert identity.user.name == "测试用户"
    assert len(identity.user.accounts) == 1
    assert identity.user.accounts[0].platform == "qq"
    assert identity.user.accounts[0].id == "12345"
    assert identity.persona.immutable == "你是一个测试用的 Bot。"
    assert identity.persona.mutable == ""
    assert identity.persona.behavior_style == "简短回应。"
    assert identity.persona.expression_style == "口语化。"
    assert identity.persona.locks.mutable is False


def test_load_identity_file_not_found(paths: AureusPaths):
    """测试身份文件不存在时抛出异常。"""
    service = IdentityService(paths)
    with pytest.raises(IdentityFileNotFoundError) as exc_info:
        service.load_identity()
    assert "身份配置文件不存在" in str(exc_info.value)


def test_load_identity_missing_required_field(paths: AureusPaths):
    """测试缺少必需字段时抛出验证异常。"""
    paths.identity_file.parent.mkdir(parents=True, exist_ok=True)
    # 缺少 facts.name
    invalid_content = textwrap.dedent(
        """\
        [facts]
        birthday = "2024-01-01"

        [user]
        name = "测试用户"

        [persona]
        immutable = "测试"
        """
    )
    paths.identity_file.write_text(invalid_content, encoding="utf-8")

    service = IdentityService(paths)
    with pytest.raises(IdentityValidationError) as exc_info:
        service.load_identity()
    assert "配置不符合规范" in str(exc_info.value)


def test_load_identity_extra_field_forbidden(paths: AureusPaths):
    """测试额外字段被禁止（extra=forbid）。"""
    paths.identity_file.parent.mkdir(parents=True, exist_ok=True)
    invalid_content = textwrap.dedent(
        """\
        [facts]
        name = "测试Bot"
        birthday = "2024-01-01"
        unknown_field = "不允许的字段"

        [user]
        name = "测试用户"

        [persona]
        immutable = "测试"
        """
    )
    paths.identity_file.write_text(invalid_content, encoding="utf-8")

    service = IdentityService(paths)
    with pytest.raises(IdentityValidationError) as exc_info:
        service.load_identity()
    assert "配置不符合规范" in str(exc_info.value)


def test_load_identity_empty_optional_fields(paths: AureusPaths):
    """测试可留空的字段。"""
    paths.identity_file.parent.mkdir(parents=True, exist_ok=True)
    content = textwrap.dedent(
        """\
        [facts]
        name = "Bot"
        gender = ""
        birthday = "2024-01-01"

        [user]
        name = "用户"

        [persona]
        immutable = "基础人设"
        """
    )
    paths.identity_file.write_text(content, encoding="utf-8")

    service = IdentityService(paths)
    identity = service.load_identity()

    assert identity.facts.gender == ""
    assert identity.user.accounts == []
    assert identity.persona.mutable == ""
    assert identity.persona.behavior_style == ""
    assert identity.persona.expression_style == ""


def test_single_instance_lock_success(paths: AureusPaths):
    """测试单实例锁正常获取和释放。"""
    service = IdentityService(paths)
    with service.acquire_lock():
        # 持有锁期间，另一个服务无法获取
        service2 = IdentityService(paths)
        with pytest.raises(SingleInstanceLockError) as exc_info:
            service2.acquire_lock(timeout=0.1).__enter__()
        assert "另一个 Aureus 实例正在运行" in str(exc_info.value)
    # 释放后可以重新获取
    with service.acquire_lock():
        pass


def test_lock_file_location(paths: AureusPaths):
    """测试锁文件放在数据根目录下。"""
    service = IdentityService(paths)
    expected_lock_file = paths.home / "aureus.lock"
    with service.acquire_lock():
        assert expected_lock_file.exists()
