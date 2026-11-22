"""公共地基：保留注释和换行，校验通过后原子写回 TOML（D84）。"""

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.toml_document import TOMLDocument


def apply_toml_edit[T](
    path: Path,
    edit: Callable[[TOMLDocument], None],
    validate: Callable[[dict[str, Any]], T],
) -> T:
    """在文件此刻的内容上修改，校验通过才替换；失败保留原文件并清理临时文件。

    返回校验结果。文件不存在时从空文档开始。解析、修改、校验和磁盘异常交给调用方处理。
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        text = ""
    newline = "\r\n" if "\r\n" in text else "\n"
    document = tomlkit.parse(text)
    edit(document)
    result = validate(document.unwrap())
    rendered = tomlkit.dumps(document).replace("\r\n", "\n").replace("\n", newline)
    if rendered == text:
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(rendered.encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return result
