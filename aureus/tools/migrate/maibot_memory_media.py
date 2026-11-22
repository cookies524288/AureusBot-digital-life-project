"""归档 MaiBot 和 A_Memorix 的可验证附件，保留缺失与远端引用。"""

import binascii
import json
import re
from base64 import b64decode
from collections.abc import Iterator
from hashlib import file_digest, md5, sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

import msgpack

from aureus.memory import MemoryEvidence
from aureus.tools.migrate.maibot_memory_rows import StagedRow

_HASH = re.compile(r"(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{64})")
_BLOCK_SIZE = 1024 * 1024
_END = object()


def message_components(row: StagedRow) -> list[object]:
    """读取现行 MessagePack 与旧 JSON 的 MaiBot 消息组件数组。"""
    packed = row.values.get("raw_content")
    if packed is None:
        return []
    if isinstance(packed, str):
        content = json.loads(packed)
    elif isinstance(packed, dict):
        payload = b64decode(packed["blob_base64"], validate=True)
        # JSON 数组以 ASCII 左方括号开头，MessagePack 数组有独立的类型标记。
        if payload.lstrip().startswith(b"["):
            content = json.loads(payload)
        else:
            content = msgpack.unpackb(payload, strict_map_key=False)
    else:
        raise ValueError("invalid_message_components")
    if not isinstance(content, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("type"), str) for entry in content
    ):
        raise ValueError("invalid_message_components")
    return content


def components(content: list[object]) -> Iterator[dict[str, object]]:
    """按原有次序遍历消息和转发组件，保留嵌套附件。"""
    pending = iter(content)
    parents: list[Iterator[object]] = []
    while True:
        entry = next(pending, _END)
        if entry is _END:
            if not parents:
                return
            pending = parents.pop()
            continue
        if not isinstance(entry, dict):
            continue
        yield entry
        data = entry.get("data")
        if entry.get("type") == "forward" and isinstance(data, list):
            parents.append(pending)
            pending = iter(
                item
                for node in data
                if isinstance(node, dict) and isinstance(node.get("content"), list)
                for item in node["content"]
            )


class AttachmentArchive:
    """仅在显式源目录查找字节，按 SHA-256 写入目标附件库。"""

    def __init__(self, roots: dict[str, Path | None], destination: Path) -> None:
        self.roots = roots
        self.destination = destination / "attachments"
        if not self.destination.resolve().is_relative_to(destination.resolve()):
            raise ValueError("invalid_attachment_target")

    def file(self, row: StagedRow, reference: str, expected: str) -> MemoryEvidence:
        """核验原附件的源哈希并归档，只提交内容地址。"""
        root = self.roots.get(row.source.role)
        if not reference:
            return MemoryEvidence("attachment_reference_missing", expected)
        if not _HASH.fullmatch(expected):
            return MemoryEvidence("attachment_digest_missing", reference)
        if root is None:
            return MemoryEvidence("attachment_root_missing", reference)
        root = root.resolve()
        path = (root / reference).resolve()
        if not path.is_relative_to(root):
            return MemoryEvidence("attachment_outside_root", reference)
        try:
            with path.open("rb") as stream:
                return self._write(iter(lambda: stream.read(_BLOCK_SIZE), b""), expected)
        except FileNotFoundError:
            return MemoryEvidence("attachment_missing", reference)
        except OSError:
            return MemoryEvidence("attachment_unreadable", reference)

    def inline(self, encoded: str) -> MemoryEvidence:
        """归档已有的内嵌文件字节，错误时保留原件里的引用。"""
        try:
            data = b64decode(encoded, validate=True)
        except binascii.Error:
            return MemoryEvidence("attachment_invalid_base64", "file")
        return self._write(iter((data,)), None)

    def _write(self, blocks: Iterator[bytes], expected: str | None) -> MemoryEvidence:
        digest = sha256()
        legacy = md5(usedforsecurity=False) if expected and len(expected) == 32 else None
        self.destination.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=self.destination, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                for block in blocks:
                    stream.write(block)
                    digest.update(block)
                    if legacy is not None:
                        legacy.update(block)
                stream.close()
                if expected and (legacy or digest).hexdigest() != expected.lower():
                    return MemoryEvidence("attachment_digest_mismatch", expected)
                identity = digest.hexdigest()
                target = self.destination / identity
                try:
                    target.hardlink_to(temporary)
                except FileExistsError:
                    # 内容地址只能复用经验证的现有文件。
                    if target.is_symlink():
                        raise OSError("invalid_attachment_target") from None
                    with target.open("rb") as copied:
                        if file_digest(copied, "sha256").hexdigest() != identity:
                            raise OSError("invalid_attachment_target") from None
                return MemoryEvidence("attachment_archived", f"attachments/{identity}")
            finally:
                temporary.unlink(missing_ok=True)
