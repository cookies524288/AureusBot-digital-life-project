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
from dataclasses import dataclass
from typing import Any, BinaryIO, Literal

type ContentLevel = Literal["knowledge", "full"]
type BundleMode = Literal["merge", "restore"]

BUNDLE_FORMAT = "a-memorix-memory-bundle"
MAX_BUNDLE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_BUNDLE_BYTES = MAX_BUNDLE_UNCOMPRESSED_BYTES + 1024 * 1024
_BASE_MEMBERS = {"manifest.json", "knowledge.json", "checksums.json"}
_STATE_MEMBER = "state.json"


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
    """已校验的 v1 记忆包及其原始语义成员。"""

    package_id: str
    version: str
    name: str
    content_level: ContentLevel
    digest: str
    manifest: dict[str, Any]
    knowledge: dict[str, Any]
    state: dict[str, Any] | None

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


def _read_members(file: BinaryIO) -> dict[str, dict[str, Any]]:
    with zipfile.ZipFile(file, "r") as archive:
        infos = archive.infolist()
        names = {info.filename for info in infos}
        if len(names) != len(infos) or not names >= _BASE_MEMBERS:
            raise MemoryBundleError("记忆包包含重复文件项或缺少必要成员。")
        unknown = names - _BASE_MEMBERS - {_STATE_MEMBER}
        if unknown:
            raise MemoryBundleError(f"暂不支持记忆包成员：{', '.join(sorted(unknown)[:6])}")
        if any(info.file_size > MAX_BUNDLE_MEMBER_BYTES for info in infos) or (
            sum(info.file_size for info in infos) > MAX_BUNDLE_UNCOMPRESSED_BYTES
        ):
            raise MemoryBundleError("记忆包单个成员超过 256 MiB 或解压总量超过 1 GiB。")

        members: dict[str, dict[str, Any]] = {}
        computed: dict[str, str] = {}
        for info in infos:
            content, checksum = _read_archive_member(archive, info)
            value = json.loads(content)
            if not isinstance(value, dict):
                raise MemoryBundleError(f"记忆包成员不是 JSON 对象：{info.filename}")
            members[info.filename] = value
            if info.filename != "checksums.json":
                computed[info.filename] = checksum
    checksums = members["checksums.json"]
    if checksums.get("algorithm") != "sha256" or checksums.get("files") != computed:
        raise MemoryBundleError("记忆包成员 SHA-256 校验失败或清单不完整。")
    return members


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
        members = _read_members(file)
        manifest = members["manifest.json"]
        knowledge = members["knowledge.json"]
        state = members.get(_STATE_MEMBER)
        if manifest.get("format") != BUNDLE_FORMAT or manifest.get("format_version") != 1:
            raise MemoryBundleError("仅支持 A_Memorix v1 文字记忆包。")
        level = manifest.get("content_level")
        if level not in ("knowledge", "full") or (level == "full") != (state is not None):
            raise MemoryBundleError("记忆包内容级别和 state.json 不一致。")
        if knowledge.get("format") != "lpmm_openie" or knowledge.get("format_version") != 1:
            raise MemoryBundleError("不支持 knowledge.json 的格式。")
        digest = bundle_digest({"knowledge": knowledge, "state": state})
        if digest != manifest.get("content_digest"):
            raise MemoryBundleError("记忆包内容摘要不一致。")
        package_id, version, name = _identity(manifest.get("package"))
        return MemoryBundle(package_id, version, name, level, digest, manifest, knowledge, state)
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
    """按上游格式重新打包保存的安装原件，不加入本机资料。"""
    members = {
        "manifest.json": _canonical_json_bytes(bundle.manifest),
        "knowledge.json": _canonical_json_bytes(bundle.knowledge),
    }
    if bundle.state is not None:
        members[_STATE_MEMBER] = _canonical_json_bytes(bundle.state)
    checksums = {name: hashlib.sha256(content).hexdigest() for name, content in members.items()}
    members["checksums.json"] = _canonical_json_bytes({"algorithm": "sha256", "files": checksums})
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
