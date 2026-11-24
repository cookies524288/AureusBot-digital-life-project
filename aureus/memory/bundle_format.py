"""读取固定版 A_Memorix 2.0.0 的记忆包，不解压到磁盘。

流式成员校验、规范 JSON、安装编号和段落规范化移植自 MaiBot
062d45c 内置 src/A_memorix/core/runtime/services/bundle_admin_service.py 及
core/utils/hash.py（AGPL-3.0）。改为读取已上传的文件，拒绝未接入的成员。
"""

import hashlib
import json
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Literal

from aureus.memory.bundle_images import (
    IMAGE_LIMIT_BYTES,
    IMAGE_MEMBER,
    canonical_image_semantics,
    validate_images,
)

type ContentLevel = Literal["knowledge", "full"]
type BundleMode = Literal["merge", "restore"]

BUNDLE_FORMAT = "a-memorix-memory-bundle"
MAX_BUNDLE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_BUNDLE_BYTES = MAX_BUNDLE_UNCOMPRESSED_BYTES + 1024 * 1024
_BASE_MEMBERS = {"manifest.json", "knowledge.json", "checksums.json"}
_STATE_MEMBER = "state.json"
_IMAGE_JSON = "images.json"
VECTOR_MEMBERS = frozenset({"vectors/paragraphs.npz", "vectors/graph.npz", "vectors/images.npz"})
_MAX_MEMBERS = 4096
_MAX_VECTOR_MEMBER_BYTES = 16 * 1024 * 1024


class MemoryBundleError(ValueError):
    """记忆包格式无效或含未支持的资料。"""


class MemoryBundleConflictError(MemoryBundleError):
    """当前记忆库状态不允许执行此操作。"""


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def bundle_digest(value: object) -> str:
    """计算规范 JSON 的内容摘要。"""
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def paragraph_hash(text: str) -> str:
    """沿用上游对段落内容的规范化和去重键。"""
    normalized = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[^\S\n]+", " ", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryBundle:
    """已校验的 v1/v2 记忆包及其原始语义成员。"""

    package_id: str
    version: str
    name: str
    content_level: ContentLevel
    digest: str
    manifest: dict[str, Any]
    knowledge: dict[str, Any]
    state: dict[str, Any] | None
    images: dict[str, Any] | None = None
    binary_members: dict[str, bytes] = field(default_factory=dict)

    @property
    def installation_id(self) -> str:
        """Aureus 只有共享知识库，使用上游全局安装的固定编号。"""
        key = f"{self.package_id}\n{self.version}\nglobal\nglobal"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"kpi_{digest[:32]}"


def _read_archive_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[bytes, str]:
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    read_size = 0
    with archive.open(info, "r") as member:
        while chunk := member.read(1024 * 1024):
            read_size += len(chunk)
            if read_size > info.file_size or read_size > MAX_BUNDLE_MEMBER_BYTES:
                raise MemoryBundleError(f"记忆包成员解压长度异常：{info.filename}")
            digest.update(chunk)
            chunks.append(chunk)
    if read_size != info.file_size:
        raise MemoryBundleError(f"记忆包成员解压长度异常：{info.filename}")
    return b"".join(chunks), digest.hexdigest()


def _read_members(file: BinaryIO) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    with zipfile.ZipFile(file, "r") as archive:
        infos = archive.infolist()
        names = {info.filename for info in infos}
        if len(infos) > _MAX_MEMBERS or len(names) != len(infos) or not names >= _BASE_MEMBERS:
            raise MemoryBundleError("记忆包包含重复文件项或缺少必要成员。")
        binary_names = (names & VECTOR_MEMBERS) | {
            name for name in names if IMAGE_MEMBER.fullmatch(name)
        }
        unknown = names - _BASE_MEMBERS - {_STATE_MEMBER, _IMAGE_JSON} - binary_names
        if unknown:
            raise MemoryBundleError("记忆包包含未知或不受支持的成员路径。")
        if any(info.file_size > MAX_BUNDLE_MEMBER_BYTES for info in infos) or (
            sum(info.file_size for info in infos) > MAX_BUNDLE_UNCOMPRESSED_BYTES
        ):
            raise MemoryBundleError("记忆包单个成员超过 256 MiB 或解压总量超过 1 GiB。")

        members: dict[str, dict[str, Any]] = {}
        binaries: dict[str, bytes] = {}
        computed: dict[str, str] = {}
        for info in infos:
            if IMAGE_MEMBER.fullmatch(info.filename) and info.file_size > IMAGE_LIMIT_BYTES:
                raise MemoryBundleError("图片成员字节过多。")
            if info.filename in VECTOR_MEMBERS and info.file_size > _MAX_VECTOR_MEMBER_BYTES:
                raise MemoryBundleError("向量成员字节过多。")
            content, checksum = _read_archive_member(archive, info)
            if info.filename in binary_names:
                binaries[info.filename] = content
            else:
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise MemoryBundleError(f"记忆包成员不是 JSON 对象：{info.filename}")
                members[info.filename] = value
            if info.filename != "checksums.json":
                computed[info.filename] = checksum
    checksums = members["checksums.json"]
    if checksums.get("algorithm") != "sha256" or checksums.get("files") != computed:
        raise MemoryBundleError("记忆包成员 SHA-256 校验失败或清单不完整。")
    return members, binaries


def _identity(package: object) -> tuple[str, str, str]:
    if not isinstance(package, dict):
        raise MemoryBundleError("记忆包缺少包标识。")
    package_id, version, name = (package.get(key) for key in ("id", "version", "name"))
    if (
        not isinstance(package_id, str)
        or not isinstance(version, str)
        or not (
            0 < len(package_id) <= 250
            and 0 < len(version) <= 250
            and all(char.isprintable() for char in package_id + version)
            and package_id.strip() == package_id
            and version.strip() == version
        )
    ):
        raise MemoryBundleError("记忆包 ID 或版本无效。")
    return package_id, version, name.strip()[:250] if isinstance(name, str) else package_id


def load_bundle(file: BinaryIO) -> MemoryBundle:
    """在预检和安装时都重新校验 ZIP、成员清单和语义摘要。"""
    try:
        members, binaries = _read_members(file)
        manifest = members["manifest.json"]
        knowledge = members["knowledge.json"]
        state = members.get(_STATE_MEMBER)
        images = members.get(_IMAGE_JSON)
        version_number = manifest.get("format_version")
        if (
            manifest.get("format") != BUNDLE_FORMAT
            or type(version_number) is not int
            or version_number not in (1, 2)
        ):
            raise MemoryBundleError("仅支持 A_Memorix v1 或 v2 记忆包。")
        if (version_number == 2) != (images is not None) or (
            images is None and any(IMAGE_MEMBER.fullmatch(name) for name in binaries)
        ):
            raise MemoryBundleError("记忆包版本和图片成员不一致。")
        level = manifest.get("content_level")
        if level not in ("knowledge", "full") or (level == "full") != (state is not None):
            raise MemoryBundleError("记忆包内容级别和 state.json 不一致。")
        if knowledge.get("format") != "lpmm_openie" or knowledge.get("format_version") != 1:
            raise MemoryBundleError("不支持 knowledge.json 的格式。")
        semantic_payload = {"knowledge": knowledge, "state": state}
        if images is not None:
            try:
                validate_images(images, binaries)
            except ValueError as error:
                raise MemoryBundleError(str(error)) from error
            semantic_payload["images"] = canonical_image_semantics(images)
        components = manifest.get("components")
        if version_number == 2 or binaries:
            actual = (members.keys() - _BASE_MEMBERS) | {"knowledge.json"} | binaries.keys()
            if (
                not isinstance(components, list)
                or not all(isinstance(name, str) for name in components)
                or len(components) != len(actual)
                or set(components) != actual
            ):
                raise MemoryBundleError("记忆包扩展成员与清单不一致。")
        digest = bundle_digest(semantic_payload)
        if digest != manifest.get("content_digest"):
            raise MemoryBundleError("记忆包内容摘要不一致。")
        package_id, version, name = _identity(manifest.get("package"))
        return MemoryBundle(
            package_id, version, name, level, digest, manifest, knowledge, state, images, binaries
        )
    except (
        zipfile.BadZipFile,
        zlib.error,
        RuntimeError,
        NotImplementedError,
        UnicodeError,
        RecursionError,
        json.JSONDecodeError,
    ) as error:
        raise MemoryBundleError("记忆包不是可读取的 ZIP 或 JSON 文件。") from error


def write_bundle(bundle: MemoryBundle, output: BinaryIO) -> None:
    """按固定版规范压缩已确立的成员并逐个写入校验和。"""
    members = {
        "manifest.json": _canonical_json_bytes(bundle.manifest),
        "knowledge.json": _canonical_json_bytes(bundle.knowledge),
    }
    if bundle.state is not None:
        members[_STATE_MEMBER] = _canonical_json_bytes(bundle.state)
    if bundle.images is not None:
        members[_IMAGE_JSON] = _canonical_json_bytes(bundle.images)
    members.update(bundle.binary_members)
    checksums = {name: hashlib.sha256(content).hexdigest() for name, content in members.items()}
    members["checksums.json"] = _canonical_json_bytes({"algorithm": "sha256", "files": checksums})
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
