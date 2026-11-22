"""器官提示词的登记、纯文字渲染与用户覆盖（D90）。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from string import Template
from threading import Lock

from loguru import logger
from tomlkit.exceptions import ParseError
from tomlkit.toml_document import TOMLDocument

from aureus.common.toml_edit import apply_toml_edit


class PromptTemplateError(ValueError):
    """提交的提示词模板无效。"""


class PromptTemplate:
    """用标准库的纯文字占位语法登记可编辑片段。"""

    def __init__(self, key: str, label: str, default: str, *, editable: bool = True) -> None:
        self.key = key
        self.label = label
        self.default = Template(default)
        self.editable = editable
        self.variables = tuple(self.default.get_identifiers())
        self.compile(default)

    def compile(self, source: str) -> Template:
        """校验语法和变量集合。变量值在运行时单次插入。"""
        template = Template(source)
        if not source.strip() or not template.is_valid():
            raise PromptTemplateError("请输入有效模板。变量用 $名称，美元符号用 $$。")
        variables = set(template.get_identifiers())
        required = set(self.variables)
        if unknown := variables - required:
            raise PromptTemplateError("未知变量：" + "、".join(sorted(unknown)))
        if missing := required - variables:
            raise PromptTemplateError("缺少变量：" + "、".join(sorted(missing)))
        return template

    def fill(self, **values: str) -> PromptText:
        """绑定材料，由模型适配器或预览选择当前模板版本。"""
        return PromptText(self.default.substitute(values), template=self, values=values)


class PromptText(str):
    """携带模板和输入的字符串，旧的假器官和调用者仍可直接使用默认文字。"""

    template: PromptTemplate | None
    values: Mapping[str, str]
    parts: tuple[str, ...]
    separator: str

    def __new__(
        cls,
        default: str,
        *,
        template: PromptTemplate | None = None,
        values: Mapping[str, str] | None = None,
        parts: Sequence[str] = (),
        separator: str = "",
    ) -> PromptText:
        """创建默认文本及其渲染材料。"""
        text = super().__new__(cls, default)
        text.template = template
        text.values = values or {}
        text.parts = tuple(parts)
        text.separator = separator
        return text

    def render(self, overrides: Mapping[str, Template]) -> str:
        """沿同一份组合渲染，使用单次请求取得的覆盖快照。"""
        if self.template is not None:
            template = overrides.get(self.template.key, self.template.default)
            return template.substitute(
                {name: render_prompt(value, overrides) for name, value in self.values.items()}
            )
        return self.separator.join(render_prompt(part, overrides) for part in self.parts)

    def template_keys(self) -> set[str]:
        """返回本片段及内含片段的登记名。"""
        keys = {self.template.key} if self.template is not None else set()
        for part in (*self.parts, *self.values.values()):
            if isinstance(part, PromptText):
                keys.update(part.template_keys())
        return keys


def render_prompt(text: str, overrides: Mapping[str, Template]) -> str:
    """渲染登记模板或原样交付普通文字。"""
    return text.render(overrides) if isinstance(text, PromptText) else text


def join_prompt(parts: Sequence[str], separator: str = "\n\n") -> PromptText:
    """拼接片段并保留登记模板，直到最终发送时再渲染。"""
    return PromptText(separator.join(parts), parts=parts, separator=separator)


def prompt_template_keys(*texts: str) -> set[str]:
    """查询消息使用的模板，包括内含的共用片段。"""
    return join_prompt(texts).template_keys()


class PromptStore:
    """用户数据目录中的覆盖文件，读取请求只使用内存中的有效版本。"""

    def __init__(self, path: Path, templates: Sequence[PromptTemplate]) -> None:
        self._path = path
        self.templates = {template.key: template for template in templates}
        self._lock = Lock()
        self._overrides: dict[str, Template] = {}
        try:
            self._overrides = self._compile(tomllib.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            logger.warning("prompts.toml 无法加载，使用默认模板（{}）", type(error).__name__)

    def snapshot(self) -> Mapping[str, Template]:
        """同一请求的所有消息使用同一份模板版本。"""
        return self._overrides

    def _compile(self, data: Mapping[str, object]) -> dict[str, Template]:
        compiled: dict[str, Template] = {}
        for key, source in data.items():
            if (
                key not in self.templates
                or not self.templates[key].editable
                or not isinstance(source, str)
            ):
                raise PromptTemplateError("覆盖文件包含未登记的模板或非文本值。")
            compiled[key] = self.templates[key].compile(source)
        return compiled

    def write(self, key: str, source: str | None) -> None:
        """校验并原子持久化，成功后发布新快照。传 None 恢复默认。"""
        if key not in self.templates:
            raise KeyError(key)
        if source is not None and not self.templates[key].editable:
            raise PromptTemplateError("此片段属于固定协议或安全规则。")

        def edit(document: TOMLDocument) -> None:
            if source is None:
                document.pop(key, None)
            else:
                document[key] = source

        with self._lock:
            compiled: dict[str, Template] = {}

            def validate(data: dict[str, object]) -> None:
                compiled.update(self._compile(data))

            try:
                apply_toml_edit(self._path, edit, validate)
            except (OSError, UnicodeError, ParseError):
                raise PromptTemplateError("无法读写 prompts.toml，模板未更新。") from None
            self._overrides = compiled
