"""器官层：按名字取服务商的密钥。

models.toml 里只写密钥的名字（D30），这里负责把名字换成密钥。
来源按优先级排列（D36，设计见 docs/15-model-management.md 第 7 节）：
环境变量 > 加密的密钥库 secrets.vault > 旧的明文 .env。
密钥用 SecretStr 保存，打印出来只显示星号。
"""

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from dotenv import dotenv_values
from loguru import logger
from pydantic import SecretStr

from aureus.common.vault import SecretVault

# 密钥名必须以这个开头：不会和别的程序的环境变量混在一起，
# 也能挡住“把密钥本身粘贴进配置文件”的失误（见 model_config.py）。
KEY_NAME_PREFIX = "AUREUS_KEY_"


class KeySource(Protocol):
    """一个密钥来源。以后新增存放方式，只要加一个实现这两个方法的类（D36）。"""

    def get(self, name: str) -> str | None:
        """取一个密钥原文；这个来源里没有就返回 None。"""
        ...

    def names(self) -> list[str]:
        """列出这个来源里已填写的密钥名。"""
        ...


def _clean(value: str | None) -> str | None:
    # 只有空白等于没填：这样的密钥在调用前就被拒绝，不会发出空请求。
    if value is None:
        return None
    return value.strip() or None


class MappingSource:
    """从一张 {名字: 值} 表里取密钥：环境变量、.env 和解开后的密钥库都是这种表。"""

    def __init__(self, values: Mapping[str, str | None], *, only_key_names: bool = False) -> None:
        self._values = values
        # 环境变量和 .env 里有很多别的变量，列名字时只认 AUREUS_KEY_ 开头的；
        # 密钥库是我们自己的，里面每一条都算密钥。
        self._only_key_names = only_key_names

    def get(self, name: str) -> str | None:
        """取一个密钥原文；没有、或者只有空白时返回 None。"""
        return _clean(self._values.get(name))

    def names(self) -> list[str]:
        """列出已填写的密钥名（排序）。"""
        candidates = (
            name
            for name in self._values
            if not self._only_key_names or name.startswith(KEY_NAME_PREFIX)
        )
        return sorted(name for name in candidates if self.get(name) is not None)


class KeyResolver:
    """把密钥名换成密钥：按优先级依次问各个来源，取第一个非空的值。"""

    def __init__(
        self,
        env_file: Path,
        environ: Mapping[str, str] | None = None,
        vault: SecretVault | None = None,
    ) -> None:
        """创建解析器。

        Args:
            env_file: 旧的明文 .env 文件，不存在也可以。
            environ: 环境变量表，默认读真实的环境变量；测试时可以传入假的。
            vault: 加密的密钥库；不传就只用环境变量和 .env。

        Raises:
            VaultError: 密钥库存在但打不开。这里直接报错、不回退到 .env：
                否则主密钥丢了，会悄悄用上旧的明文（D36）。
        """
        # 文件不存在时 dotenv_values 返回空表；值为 None 表示只有变量名、没有等号和值。
        file_values = {
            name: value for name, value in dotenv_values(env_file).items() if value is not None
        }
        self._dotenv = MappingSource(file_values, only_key_names=True)
        live_environment = os.environ if environ is None else environ
        sources: list[KeySource] = [MappingSource(live_environment, only_key_names=True)]
        # 主密钥本身也要从日志里隐藏；它不是某一条密钥，所以单独记着。
        self._extra_hidden: list[str] = []
        if vault is not None:
            # 启动时解锁一次，之后只用内存里的副本：不在每次调用时读文件、做解密。
            sources.append(MappingSource(vault.load()))
            master_key = vault.master_key()
            if master_key is not None:
                self._extra_hidden.append(master_key.decode("ascii"))
        sources.append(self._dotenv)
        self._sources = tuple(sources)

    def get(self, env_name: str) -> SecretStr | None:
        """取一个密钥。

        Args:
            env_name: 密钥名，比如 AUREUS_KEY_EXAMPLE。

        Returns:
            密钥；所有来源里都没填、或者只有空白时返回 None（调用前就拒绝，不发空请求）。
        """
        for source in self._sources:
            value = source.get(env_name)
            if value is not None:
                return SecretStr(value)
        return None

    def secret_values(self, env_names: Iterable[str]) -> list[str]:
        """列出这些密钥名对应的密钥原文。

        Args:
            env_names: 密钥名。

        Returns:
            已填写的密钥原文列表。
        """
        secrets = (self.get(name) for name in env_names)
        return [secret.get_secret_value() for secret in secrets if secret is not None]

    def all_secret_values(self) -> list[str]:
        """列出所有来源里的全部密钥原文和主密钥本身，交给日志模块，让它们不会出现在日志里。

        不管 models.toml 有没有引用：日志过滤宁可多藏，不能漏藏（D36）。

        Returns:
            去重、排序后的原文列表。
        """
        values = {
            value
            for source in self._sources
            for name in source.names()
            if (value := source.get(name)) is not None
        }
        values.update(self._extra_hidden)
        return sorted(values)

    def plaintext_key_names(self) -> list[str]:
        """列出旧的明文 .env 里已填写的密钥名（只有名字，没有值）。"""
        return self._dotenv.names()


def warn_about_plaintext_keys(resolver: KeyResolver) -> None:
    """.env 里还有明文密钥时打警告，建议迁移。只写密钥名，不写值。

    要在日志设置好之后调用：解锁密钥库的过程本身不写日志（D36）。

    Args:
        resolver: 密钥解析器。
    """
    names = resolver.plaintext_key_names()
    if names:
        logger.warning(
            ".env 里还有明文密钥：{}。建议运行 keys migrate 搬进加密的密钥库。", ", ".join(names)
        )
