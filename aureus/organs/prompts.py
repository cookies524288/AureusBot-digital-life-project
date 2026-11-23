"""器官提示词的持久化覆盖（D90）。"""

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from string import Template
from threading import Lock

from loguru import logger
from tomlkit.exceptions import ParseError
from tomlkit.toml_document import TOMLDocument

from aureus.common.prompt_text import PromptTemplate, PromptTemplateError
from aureus.common.toml_edit import apply_toml_edit


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
