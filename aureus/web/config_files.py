"""网页配置文件服务：校验后写文件，记录重启提示，不改运行中的核心（D84）。"""

import asyncio
import logging
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, get_args

import tomlkit
from tomlkit.toml_document import TOMLDocument

from aureus.common import AureusPaths, ConfigError, apply_toml_edit
from aureus.common.vault import SecretVault, VaultError
from aureus.organs import (
    KeyResolver,
    ModelKind,
    ModelsConfig,
    OrganStrategy,
    ProviderProtocol,
    ReasoningLevel,
    validate_models_config,
)

logger = logging.getLogger(__name__)


def _read_toml(path: Path, source: str) -> tuple[bool, dict[str, Any]]:
    try:
        return True, tomllib.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return False, {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ConfigError(f"{source} 不是合法的 TOML。") from None
    except OSError:
        raise ConfigError(f"读不了 {source}。") from None


def _edit_models(document: TOMLDocument, submitted: ModelsConfig) -> None:
    """按名称复用未改的表；有改动的整条替换，顺序跟随提交。"""
    current = validate_models_config(document.unwrap(), "models.toml")
    for section in ("providers", "models"):
        old_entries = {item.name: item for item in getattr(current, section)}
        entries = getattr(submitted, section)
        if tuple(old_entries.values()) == entries:
            continue
        old_tables = {table["name"]: table for table in document.get(section, [])}
        tables = tomlkit.aot()
        for entry in entries:
            if old_entries.get(entry.name) == entry:
                tables.append(old_tables[entry.name])
            else:
                tables.append(tomlkit.item(entry.model_dump(mode="json", exclude_none=True)))
        if entries:
            document[section] = tables
        else:
            document.pop(section, None)
    if current.organs != submitted.organs or list(current.organs) != list(submitted.organs):
        previous = document.get("organs", {})
        organs = tomlkit.table(is_super_table=True)
        for name, entry in submitted.organs.items():
            organs[name] = (
                previous[name]
                if current.organs.get(name) == entry
                else entry.model_dump(mode="json", exclude_none=True)
            )
        document["organs"] = organs
    if "config_version" not in document:
        document["config_version"] = 1


def models_require_password(current: ModelsConfig, submitted: ModelsConfig) -> bool:
    """已有服务商改地址或密钥引用、新增带密钥的服务商时，要重新确认口令。"""
    previous = {provider.name: provider for provider in current.providers}
    for provider in submitted.providers:
        old = previous.get(provider.name)
        if old is None:
            if provider.api_key_env is not None:
                return True
        elif (old.base_url, old.api_key_env) != (provider.base_url, provider.api_key_env):
            return True
    return False


class ConfigService:
    """只碰文件的配置服务；所有文件方法由接口放进线程，不接触 SQLite 或心跳。"""

    def __init__(self, paths: AureusPaths) -> None:
        self.paths = paths
        self._vault = SecretVault(paths.vault_file, paths.master_key_file)
        # 来源是本进程成功的网页写入，只影响重启提示；进程重启清零（D84）。
        self.restart_required = False
        # to_thread 会让出执行权；串行处理网页写入，避免两个请求互相覆盖。
        self.write_lock = asyncio.Lock()

    def read_models(self) -> tuple[bool, ModelsConfig]:
        """读取文件的有效值；缺文件是空骨架，错误只使用相对来源名。"""
        exists, raw = _read_toml(self.paths.models_config_file, "models.toml")
        return exists, validate_models_config(raw, "models.toml")

    def view_models(self) -> dict[str, Any]:
        """模型配置、由 Literal 得到的选项和当前密钥状态，不返回密钥原文。"""
        exists, config = self.read_models()
        names = [p.api_key_env for p in config.providers if p.api_key_env is not None]
        return {
            "exists": exists,
            "config": config.model_dump(mode="json"),
            "choices": {
                "protocols": list(get_args(ProviderProtocol)),
                "kinds": list(get_args(ModelKind)),
                "strategies": list(get_args(OrganStrategy)),
                "reasoning_levels": list(get_args(ReasoningLevel)),
            },
            "key_status": self.key_status(names),
        }

    def key_status(self, names: Iterable[str]) -> dict[str, bool]:
        """按环境变量、现读密钥库、现读 .env 的顺序，只返回是否有非空密钥。"""
        try:
            resolver = KeyResolver(self.paths.env_file, vault=self._vault)
        except VaultError as error:
            raise ConfigError(self.describe_vault_error(error)) from None
        return {name: resolver.get(name) is not None for name in names}

    def describe_vault_error(self, error: VaultError) -> str:
        """保留密钥库错误的原因，去掉本机路径。"""
        return (
            str(error)
            .replace(str(self.paths.vault_file), "secrets.vault")
            .replace(str(self.paths.master_key_file), "secrets.key")
        )

    def write_models(self, config: ModelsConfig) -> None:
        """按文件当前内容写回模型配置；失败抛出异常，重启标志保持原值。"""
        apply_toml_edit(
            self.paths.models_config_file,
            lambda document: _edit_models(document, config),
            lambda raw: validate_models_config(raw, "提交的模型配置"),
        )
        self.restart_required = True

    def view_keys(self) -> dict[str, Any]:
        """只列密钥库状态和名字；打不开时不当成空库。"""
        if not self.paths.vault_file.exists():
            return {"vault": "none", "names": []}
        try:
            names = self._vault.names()
        except VaultError:
            return {"vault": "unreadable", "names": []}
        return {"vault": "ok", "names": names}

    def write_key(self, name: str, value: str) -> None:
        """录入密钥；首次创建、文件锁和加密均复用 SecretVault，不记录原文。"""
        self._vault.set(name, value)
        self.restart_required = True
        logger.info("网页录入密钥：%s", name)

    def remove_key(self, name: str) -> bool:
        """删除一个密钥；名字不存在时不写文件、不设置重启提示。"""
        removed = self._vault.remove(name)
        if removed:
            self.restart_required = True
            logger.info("网页删除密钥：%s", name)
        return removed
