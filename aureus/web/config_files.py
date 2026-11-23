"""网页配置文件服务：校验后原子写回；运行设置和密钥保留重启提示（D84、D86）。"""

import asyncio
import logging
import tomllib
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any, get_args

import tomlkit
from tomlkit.toml_document import TOMLDocument

from aureus.common import (
    AureusPaths,
    ConfigError,
    RuntimeConfig,
    apply_toml_edit,
    validate_runtime_config,
)
from aureus.common.config import CortisolConfig
from aureus.common.logging_setup import hide_secrets
from aureus.common.vault import SecretVault, VaultError
from aureus.organs import (
    BUILTIN_ORGANS,
    KeyResolver,
    ModelKind,
    ModelsConfig,
    OrganStrategy,
    ProviderProtocol,
    ReasoningLevel,
    validate_models_config,
)
from aureus.organs.model_config import ModelConfig, OrganConfig, ProviderConfig
from aureus.organs.topology import organ_connections

logger = logging.getLogger(__name__)

READONLY_CONFIG = (
    # 改错会关闭网页或改变开放范围，只能直接编辑文件（D84、2.12b）。
    "config_version",
    "web.enabled",
    "web.allow_remote",
    "web.port",
    "qq.enabled",
    "qq.endpoint",
    "qq.account_id",
    "qq.allowed_group_id",
    "qq.token_key",
    "qq.action_timeout_seconds",
    "qq.reconnect_seconds",
    *(f"cortisol.{field}" for field in CortisolConfig.model_fields),
)
PASSWORD_SECTIONS = ("safety", "web")
HIDDEN_CONFIG = frozenset(
    {
        "heartbeat.advance_biomodel",
        "heartbeat.planning_enabled",
        "heartbeat.roleplay_mode",
        "heartbeat.plan_organ",
        "heartbeat.reply_organ",
    }
)
NO_DEFAULT_CONFIG = frozenset({"exploration.search_endpoint"})
MODEL_ENTRY_TYPES = {"providers": ProviderConfig, "models": ModelConfig, "organs": OrganConfig}
ModelRename = tuple[str, str, str]


def model_defaults(section: str) -> dict[str, Any]:
    """从实际配置模型取默认值，供表单和稀疏写入共用。"""
    fields = MODEL_ENTRY_TYPES[section].model_json_schema()["properties"]
    return {name: prop["default"] for name, prop in fields.items() if "default" in prop}


class BiochemicalWriteError(ConfigError):
    """网页保存及恢复入口遇到生化配置时使用同一拒写规则（D89、D91）。"""


def check_biochemical_write(targets: Iterable[str]) -> None:
    """拒绝整节和节内任何写入目标，包括恢复时要删除的键。"""
    if any(target.partition(".")[0] == "cortisol" for target in targets):
        raise BiochemicalWriteError("生化参数只能在 config/aureus.toml 中修改或逐项恢复。")


def runtime_field_names(changes: dict[str, Any]) -> list[str]:
    """此次实际修改的字段名，供权限判断、响应和不含值的日志共用。"""
    names: list[str] = []
    for name, value in changes.items():
        if isinstance(value, dict):
            names.extend(f"{name}.{field}" for field in value)
        else:
            names.append(name)
    return names


def _read_toml(path: Path, source: str) -> tuple[bool, dict[str, Any]]:
    try:
        return True, tomllib.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return False, {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ConfigError(f"{source} 不是合法的 TOML。") from None
    except OSError:
        raise ConfigError(f"读不了 {source}。") from None


def _edit_model_table(table: Any, before: dict[str, Any], after: dict[str, Any]) -> None:
    if table.get("kind") == "vision" and (
        before["kind"] != after["kind"] or before["vision"] != after["vision"]
    ):
        # 旧版用一个值表示类型和视觉。改动其中一项时先拆开，避免旧写法盖过新值。
        table["kind"] = after["kind"]
        table["vision"] = after["vision"]
    for field, value in after.items():
        if field in before and before[field] == value:
            continue
        if value is None:
            table.pop(field, None)
        else:
            table[field] = value


def _edit_models(
    document: TOMLDocument, submitted: ModelsConfig, rename: ModelRename | None
) -> None:
    """按名称复用现有表，只写值有变化的字段，并跟随提交顺序。"""
    current = validate_models_config(document.unwrap(), "models.toml")
    for section in ("providers", "models"):
        old_entries = {item.name: item for item in getattr(current, section)}
        entries = getattr(submitted, section)
        if tuple(old_entries.values()) == entries:
            continue
        old_tables = {table["name"]: table for table in document.get(section, [])}
        tables = tomlkit.aot()
        for entry in entries:
            previous_name = (
                rename[1]
                if rename and (section, entry.name) == (rename[0], rename[2])
                else entry.name
            )
            previous = old_entries.get(previous_name)
            table = old_tables[previous_name] if previous is not None else tomlkit.table()
            before = (
                previous.model_dump(mode="json")
                if previous is not None
                else model_defaults(section)
            )
            _edit_model_table(table, before, entry.model_dump(mode="json"))
            tables.append(table)
        if entries:
            document[section] = tables
        else:
            document.pop(section, None)
    if current.organs != submitted.organs or list(current.organs) != list(submitted.organs):
        previous_tables = document.get("organs", {})
        organs = tomlkit.table(is_super_table=True)
        for name, entry in submitted.organs.items():
            previous = current.organs.get(name)
            table = previous_tables[name] if previous is not None else tomlkit.table()
            before = (
                previous.model_dump(mode="json")
                if previous is not None
                else model_defaults("organs")
            )
            _edit_model_table(table, before, entry.model_dump(mode="json"))
            organs[name] = table
        document["organs"] = organs
    if "config_version" not in document:
        document["config_version"] = 1


def models_require_password(current: ModelsConfig, submitted: ModelsConfig) -> bool:
    """已有服务商改地址或密钥引用、新增带密钥的服务商时，要重新确认密码。"""
    previous = {provider.name: provider for provider in current.providers}
    for provider in submitted.providers:
        old = previous.get(provider.name)
        if old is None:
            if provider.api_key_env is not None or provider.api_key_ref is not None:
                return True
        elif (
            old.protocol != provider.protocol and (provider.api_key_env or provider.api_key_ref)
        ) or (old.base_url, old.api_key_env, old.api_key_ref) != (
            provider.base_url,
            provider.api_key_env,
            provider.api_key_ref,
        ):
            return True
    return False


class ConfigService:
    """配置文件与密钥快照；文件方法放进线程，不接触 SQLite 或心跳。"""

    def __init__(self, paths: AureusPaths, runtime: RuntimeConfig | None = None) -> None:
        self.paths = paths
        self._runtime = runtime if runtime is not None else RuntimeConfig()
        self._vault = SecretVault(paths.vault_file, paths.master_key_file)
        self.key_resolver: KeyResolver | None = None
        # 来源是本进程成功的网页写入，只影响重启提示；进程重启清零（D84）。
        self.restart_required = False
        # to_thread 会让出执行权；串行处理网页写入，避免两个请求互相覆盖。
        self.write_lock = asyncio.Lock()

    def read_models(self) -> tuple[bool, ModelsConfig]:
        """读取文件的有效值；缺文件是空骨架，错误只使用相对来源名。"""
        exists, raw = _read_toml(self.paths.models_config_file, "models.toml")
        return exists, validate_models_config(raw, "models.toml")

    @property
    def runtime(self) -> RuntimeConfig:
        """当前进程生效的运行设置。"""
        return self._runtime

    def view_models(self) -> dict[str, Any]:
        """模型配置、由 Literal 得到的选项和当前密钥状态，不返回密钥原文。"""
        exists, config = self.read_models()
        names = [p.api_key_env for p in config.providers if p.api_key_env is not None]
        refs = [p.api_key_ref for p in config.providers if p.api_key_ref is not None]
        return {
            "exists": exists,
            "config": config.model_dump(mode="json"),
            "defaults": {section: model_defaults(section) for section in MODEL_ENTRY_TYPES},
            "choices": {
                "protocols": list(get_args(ProviderProtocol)),
                "kinds": list(get_args(ModelKind)),
                "strategies": list(get_args(OrganStrategy)),
                "reasoning_levels": list(get_args(ReasoningLevel)),
            },
            "key_status": self.key_status(names, refs),
            "key_entries": self.view_keys()["entries"],
            "builtin_organs": list(BUILTIN_ORGANS),
            "organ_connections": organ_connections(
                config.organs, self._runtime, config.configured_text_organs()
            ),
            "warnings": config.warnings(),
        }

    def key_status(self, names: Iterable[str], refs: Iterable[str] = ()) -> dict[str, bool]:
        """按环境变量、现读密钥库、现读 .env 的顺序，只返回是否有非空密钥。"""
        try:
            resolver = KeyResolver(self.paths.env_file, vault=self._vault)
        except VaultError as error:
            raise ConfigError(self.describe_vault_error(error)) from None
        return {
            **{name: resolver.get(name) is not None for name in names},
            **{ref: resolver.get_vault(ref) is not None for ref in refs},
        }

    def describe_vault_error(self, error: VaultError) -> str:
        """保留密钥库错误的原因，去掉本机路径。"""
        return (
            str(error)
            .replace(str(self.paths.vault_file), "secrets.vault")
            .replace(str(self.paths.master_key_file), "secrets.key")
        )

    def write_models(self, config: ModelsConfig, rename: ModelRename | None = None) -> None:
        """按文件当前内容写回模型配置；由接口热重载，不设置重启标志（D86）。"""
        apply_toml_edit(
            self.paths.models_config_file,
            lambda document: _edit_models(document, config, rename),
            lambda raw: validate_models_config(raw, "提交的模型配置"),
        )

    def view_keys(self) -> dict[str, Any]:
        """只列密钥库状态和名字；打不开时不当成空库。"""
        if not self.paths.vault_file.exists():
            return {"vault": "none", "names": [], "entries": []}
        try:
            entries = self._vault.load_entries()
        except VaultError:
            return {"vault": "unreadable", "names": [], "entries": []}
        entries.sort(key=lambda entry: entry.name)
        return {
            "vault": "ok",
            "names": [e.name for e in entries],
            "entries": [{"id": e.id, "name": e.name, "kind": e.kind} for e in entries],
        }

    def _keys_changed(self) -> None:
        entries, master_key = self._vault.load_entries(), self._vault.master_key()
        hide_secrets(e.value.get_secret_value() for e in entries)
        if master_key is not None:
            hide_secrets([master_key.decode("ascii")])
        if self.key_resolver is not None:
            self.key_resolver.replace_vault(entries, master_key)
        else:
            self.restart_required = True

    def create_key(self, name: str, kind: str, value: str) -> str:
        """新建类型化记录；不允许覆盖已有名称。"""
        hide_secrets([value, value.strip()])
        entry = self._vault.create(name, kind, value)
        self._keys_changed()
        logger.info("网页新建密钥")
        return entry.id

    def rename_key(self, entry_id: str, name: str) -> bool:
        """修改名称后同步内存，引用的 id 不变。"""
        renamed = self._vault.rename(entry_id, name)
        if renamed:
            self._keys_changed()
            logger.info("网页修改密钥名称")
        return renamed

    def update_key(self, entry_id: str, value: str) -> bool:
        """保存新值后同步内存中的密钥库。"""
        # 在途请求仍可能持有旧值，继续过滤两份原文。
        hide_secrets(self._vault.load().values())
        hide_secrets([value, value.strip()])
        updated = self._vault.update_value(entry_id, value)
        if updated:
            self._keys_changed()
            logger.info("网页修改密钥值")
        return updated

    def write_key(self, name: str, value: str) -> None:
        """录入密钥；首次创建、文件锁和加密均复用 SecretVault，不记录原文。"""
        hide_secrets([value, value.strip()])
        self._vault.set(name, value)
        self._keys_changed()
        logger.info("网页录入密钥：%s", name)

    def remove_key(self, name: str, *, by_id: bool = False) -> bool:
        """删除一个密钥；名字不存在时不写文件、不设置重启提示。"""
        # 先加入过滤，再删除，进行中的请求仍可能持有原文。
        hide_secrets(self._vault.load().values())
        removed = self._vault.remove_entry(name) if by_id else self._vault.remove(name)
        if removed:
            self._keys_changed()
            logger.info("网页删除密钥：%s", name)
        return removed

    def read_runtime(self) -> tuple[bool, RuntimeConfig]:
        """读取文件的有效运行配置，缺文件时使用默认值。"""
        exists, raw = _read_toml(self.paths.runtime_config_file, "aureus.toml")
        return exists, validate_runtime_config(raw, "aureus.toml")

    def view_runtime(self) -> dict[str, Any]:
        """提供当前文件配置、自动生成的 schema、只读项和需密码的节。"""
        exists, config = self.read_runtime()
        values = config.model_dump(mode="json")
        schema = RuntimeConfig.model_json_schema()
        for key in HIDDEN_CONFIG:
            section, _, field = key.partition(".")
            definition = schema["properties"][section]["$ref"].rsplit("/", 1)[-1]
            schema["$defs"][definition]["properties"].pop(field, None)
            values[section].pop(field, None)
        return {
            "exists": exists,
            "config": values,
            "schema": schema,
            "readonly": list(READONLY_CONFIG),
            "no_default": sorted(NO_DEFAULT_CONFIG),
            "password_sections": list(PASSWORD_SECTIONS),
        }

    def changed_runtime(self, changes: dict[str, Any]) -> dict[str, Any]:
        """按当前文件的有效值去掉没变的字段；布尔不能冒充整数。"""
        _, config = self.read_runtime()
        current = config.model_dump(mode="json")
        changed: dict[str, Any] = {}

        def same(before: Any, after: Any) -> bool:
            return type(before) is type(after) and before == after

        for name, value in changes.items():
            if isinstance(value, dict):
                previous = current.get(name, {})
                if not isinstance(previous, dict):
                    previous = {}
                fields = {
                    field: item
                    for field, item in value.items()
                    if not same(previous.get(field), item)
                }
                if fields:
                    changed[name] = fields
            elif not same(current.get(name), value):
                changed[name] = value
        return changed

    def write_runtime(self, changes: dict[str, Any]) -> None:
        """只修改提交的字段，保留其他内容和注释；成功后只记字段名并提示重启。"""
        check_biochemical_write(changes)

        def edit(document: TOMLDocument) -> None:
            for name, value in changes.items():
                if isinstance(value, dict):
                    if not isinstance(document.get(name), Mapping):
                        document[name] = tomlkit.table()
                    for field, item in value.items():
                        document[name][field] = item
                else:
                    document[name] = value

        apply_toml_edit(
            self.paths.runtime_config_file,
            edit,
            lambda raw: validate_runtime_config(raw, "提交的配置"),
        )
        self.restart_required = True
        logger.info("网页修改运行配置：%s", "、".join(runtime_field_names(changes)))

    def restore_runtime(self, key: str) -> tuple[Any, bool]:
        """仅删除一个运行配置覆盖，返回有效值和覆盖是否存在。"""
        check_biochemical_write([key])
        if key in NO_DEFAULT_CONFIG or key in HIDDEN_CONFIG:
            raise ConfigError("这项设置没有可恢复的默认值。")
        section, separator, field = key.rpartition(".")
        defaults = RuntimeConfig().model_dump(mode="json")
        fields = defaults.get(section) if separator else defaults
        if not isinstance(fields, dict) or field not in fields or isinstance(fields[field], dict):
            raise ConfigError("这项设置没有可恢复的默认值。")
        changed = False

        def edit(document: TOMLDocument) -> None:
            nonlocal changed
            table = document.get(section, {}) if section else document
            if isinstance(table, MutableMapping) and field in table:
                del table[field]
                changed = True

        config = apply_toml_edit(
            self.paths.runtime_config_file,
            edit,
            lambda raw: validate_runtime_config(raw, "恢复默认后的配置"),
        ).model_dump(mode="json")
        if changed:
            self.restart_required = True
            logger.info("网页恢复运行配置：%s", key)
        return (config[section] if section else config)[field], changed
