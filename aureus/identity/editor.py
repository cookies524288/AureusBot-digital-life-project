"""身份子系统：用户在网页上改设定，写回 identity.toml（路线图 1.10，D63 第 1、2 条）。

流程：重新读一遍 identity.toml，只改这次涉及的键，校验通过后先写临时文件、再替换原文件；
然后和版本历史对一遍，把改动记成一次「用户修改」（来源 user_file，用户决定不新增来源）。
顺序是先文件、后数据库：文件写失败就都不动；文件写成功而数据库写失败时，
下次启动 sync_from_file 会把这次改动当作用户修改补记上，不会丢。

用 tomlkit 而不是 tomllib 写：它能保留用户手写的注释和排版。
是否允许改、要不要再输密码，由调用方（网页服务）决定；这里只管改。
"""

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import ValidationError
from tomlkit.exceptions import TOMLKitError
from tomlkit.toml_document import TOMLDocument

from aureus.common import apply_toml_edit

from .history import IdentityHistory, IdentityLayer
from .models import Facts, Identity, PersonaLocks, User
from .service import IdentityError, IdentityValidationError

# 用文字存放的四层人设；身份事实另有 facts 和 user 两张表。
TEXT_LAYERS = (
    IdentityLayer.IMMUTABLE,
    IdentityLayer.MUTABLE,
    IdentityLayer.BEHAVIOR_STYLE,
    IdentityLayer.EXPRESSION_STYLE,
)
_LOCKABLE_LAYERS = ("mutable", "behavior_style", "expression_style")


class IdentityEditError(IdentityError):
    """这次修改没有做成。消息是中文，不回显填进去的内容。"""


@dataclass(frozen=True)
class IdentityEdit:
    """一次修改请求：哪些层要改成什么。没写的部分保持原样。"""

    text: Mapping[IdentityLayer, str] = field(default_factory=dict)  # 四层文字人设
    facts: Facts | None = None  # 身份事实：名字、昵称、性别、生日
    user: User | None = None  # 用户的称呼和账号，和身份事实记在同一层
    locks: PersonaLocks | None = None  # 三层的锁定开关
    restore_lock: IdentityLayer | None = None

    def requires_password(self) -> bool:
        """改的是底线（身份事实、不变人格）吗？是的话，调用方要先让用户再输一次密码。"""
        return (
            self.facts is not None or self.user is not None or IdentityLayer.IMMUTABLE in self.text
        )


@dataclass(frozen=True)
class IdentityEditResult:
    """一次修改的结果。"""

    identity: Identity  # 此刻生效的身份
    changed_layers: tuple[IdentityLayer, ...]  # 内容真的变了的层；锁定开关不记版本，不在其中
    up_to_version_id: int  # 记到了哪个版本；时间线记录里写它，醒来时就不会重复通知


class IdentityEditor:
    """把用户的修改写回 identity.toml，并记进版本历史。"""

    def __init__(self, path: Path, history: IdentityHistory) -> None:
        self._path = path
        self._history = history

    def apply(self, edit: IdentityEdit) -> IdentityEditResult:
        """做一次修改。

        Raises:
            IdentityEditError: 请求不合法、文件读不了或写不进去；文件保持原样。
            IdentityValidationError: 改完的身份不符合规范；文件保持原样。
            sqlite3.Error: 文件已经写好，但版本历史没写进去；下次启动会补记。
        """
        validated: list[Identity] = []

        def update(document: TOMLDocument) -> None:
            try:
                _apply_to_document(document, edit)
            except (TOMLKitError, TypeError, ValueError) as error:
                raise IdentityEditError("这次修改里有写不进 TOML 的内容，没有改动。") from error

        def validate(raw: dict[str, Any]) -> None:
            try:
                identity = Identity.model_validate(raw)
            except ValidationError as error:
                raise IdentityValidationError(error) from error
            if not identity.persona.immutable.strip():
                raise IdentityEditError("不变人格不能是空的。")
            validated.append(identity)

        try:
            # 身份文件必须已存在；模型和运行配置才支持从空文档创建。
            self._path.stat()
        except OSError as error:
            raise IdentityEditError(f"读不了 {self._path}：{error}") from error
        before = self._latest_version_id()
        try:
            apply_toml_edit(self._path, update, validate)
        except OSError as error:
            raise IdentityEditError(f"读不了或写不进 {self._path}：{error}") from error
        except (UnicodeDecodeError, TOMLKitError) as error:
            raise IdentityEditError(f"{self._path} 不是合法的 TOML，没有改动。") from error
        [new_identity] = validated
        effective = self._history.sync_from_file(new_identity, edited=edit.text.keys())
        changes = self._history.user_changes_after(before)
        layers = tuple(dict.fromkeys(change.layer for change in changes))
        up_to = changes[-1].id if changes else before
        return IdentityEditResult(identity=effective, changed_layers=layers, up_to_version_id=up_to)

    def _latest_version_id(self) -> int:
        latest = self._history.versions(None, limit=1)
        return latest[0].id if latest else 0


def _table(document: Any, name: str) -> Any:
    table = document.get(name)
    if table is None:
        raise IdentityEditError(f"identity.toml 里没有 [{name}] 一节，没有改动。")
    return table


def _apply_to_document(document: Any, edit: IdentityEdit) -> None:
    """把修改请求落到 TOML 文档上；只动涉及的键。"""
    if edit.text or edit.locks is not None or edit.restore_lock is not None:
        persona = _table(document, "persona")
        for layer, content in edit.text.items():
            if layer not in TEXT_LAYERS:
                raise IdentityEditError(f"{layer.describe()}不是文字人设，不能这样改。")
            # 和模板一样写成三引号多行字符串；首尾空白不算内容（见 history.file_contents）。
            persona[layer.value] = tomlkit.string(content.strip() + "\n", multiline=True)
        if edit.locks is not None:
            _apply_locks(persona, edit.locks)
        if edit.restore_lock is not None:
            name = edit.restore_lock.value
            if name not in _LOCKABLE_LAYERS:
                raise IdentityEditError("这一层没有可恢复的锁定开关。")
            table = persona.get("locks")
            if isinstance(table, MutableMapping):
                table.pop(name, None)
    if edit.facts is not None:
        facts = _table(document, "facts")
        facts["name"] = edit.facts.name
        facts["alias_names"] = list(edit.facts.alias_names)
        if "gender" in edit.facts.model_fields_set:
            facts["gender"] = edit.facts.gender
        facts["birthday"] = edit.facts.birthday
    if edit.user is not None:
        user = _table(document, "user")
        user["name"] = edit.user.name
        user["accounts"] = _accounts_array(edit.user)


def _apply_locks(persona: Any, locks: PersonaLocks) -> None:
    table = persona.get("locks")
    try:
        current = PersonaLocks.model_validate(table if table is not None else {})
    except ValidationError as error:
        raise IdentityValidationError(error) from error
    for name in _LOCKABLE_LAYERS:
        value = getattr(locks, name)
        if value != getattr(current, name):
            if table is None:
                table = tomlkit.table()
                persona["locks"] = table
            table[name] = value


def _accounts_array(user: User) -> Any:
    """账号写成一行一个的内联表，和模板里的写法一致。"""
    accounts = tomlkit.array()
    accounts.multiline(True)
    for account in user.accounts:
        entry = tomlkit.inline_table()
        entry["platform"] = account.platform
        entry["id"] = account.id
        accounts.append(entry)
    return accounts
