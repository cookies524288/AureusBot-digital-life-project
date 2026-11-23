"""纯文字提示词的登记和组合，不读取运行配置。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from string import Template


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
