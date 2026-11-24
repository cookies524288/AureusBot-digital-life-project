"""只解码有边界的数值 NPZ 成员，并在已配置空间内提供可逆向量读取。"""

import ast
import json
import math
import re
import sqlite3
import struct
import zipfile
from collections.abc import Collection, Mapping, Sequence
from io import BytesIO
from typing import Any

from aureus.memory.bundle_format import VECTOR_MEMBERS, MemoryBundle, MemoryBundleError
from aureus.memory.bundle_image_projection import semantic_targets
from aureus.memory.history import HistoricalMemory, MemorySource

MAX_VECTOR_BYTES = 16 * 1024 * 1024
_MAX_HEADER = 32 * 1024
_MAX_IDS = 100_000
_MAX_DIMENSION = 8192
_NPY_MAGIC = b"\x93NUMPY"
_DURABLE_FLOAT = "<f"
_NUMBER_TYPES = {
    "f2": "e",
    "f4": "f",
    "f8": "d",
    "i1": "b",
    "i2": "h",
    "i4": "i",
    "i8": "q",
    "u1": "B",
    "u2": "H",
    "u4": "I",
    "u8": "Q",
}


def _header(
    member: Any, info: zipfile.ZipInfo, dimensions: int
) -> tuple[str, tuple[int, ...], int]:
    if member.read(6) != _NPY_MAGIC:
        raise ValueError
    version = member.read(2)
    if version not in (b"\x01\x00", b"\x02\x00", b"\x03\x00"):
        raise ValueError
    size_bytes = 2 if version[0] == 1 else 4
    size = int.from_bytes(member.read(size_bytes), "little")
    if not 0 < size <= _MAX_HEADER:
        raise ValueError
    header = ast.literal_eval(member.read(size).decode("utf8" if version[0] == 3 else "latin1"))
    if not isinstance(header, dict) or header.keys() != {"descr", "fortran_order", "shape"}:
        raise ValueError
    dtype, fortran, shape = header["descr"], header["fortran_order"], header["shape"]
    if (
        not isinstance(dtype, str)
        or fortran is not False
        or not isinstance(shape, tuple)
        or len(shape) != dimensions
    ):
        raise ValueError
    if (
        any(type(value) is not int or value <= 0 for value in shape)
        or shape[0] > _MAX_IDS
        or shape[-1] > (_MAX_IDS if dimensions == 1 else _MAX_DIMENSION)
    ):
        raise ValueError
    return dtype, shape, info.file_size - 8 - size_bytes - size


def _decode_ids(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> list[str]:
    with archive.open(info) as member:
        dtype, shape, available = _header(member, info, 1)
        match = re.fullmatch(r"([<>])U([1-9][0-9]{0,2})", dtype)
        if match is None:
            raise ValueError
        order, width = match.groups()
        size = int(width) * 4
        if shape[0] * size != available:
            raise ValueError
        encoding = "utf-32-le" if order == "<" else "utf-32-be"
        identifiers = [member.read(size).decode(encoding).rstrip("\0") for _ in range(shape[0])]
        if (
            any(not key or "\0" in key for key in identifiers)
            or len(set(identifiers)) != len(identifiers)
            or member.read(1)
        ):
            raise ValueError
        return identifiers


def _decode_vectors(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, count: int
) -> tuple[int, list[bytes]]:
    with archive.open(info) as member:
        dtype, shape, available = _header(member, info, 2)
        if dtype[:1] not in ("<", ">", "|") or dtype[1:] not in _NUMBER_TYPES or shape[0] != count:
            raise ValueError
        column_count = shape[1]
        reader = struct.Struct(
            (">" if dtype.startswith(">") else "<") + _NUMBER_TYPES[dtype[1:]] * column_count
        )
        writer = struct.Struct(_DURABLE_FLOAT[0] + _DURABLE_FLOAT[1] * column_count)
        if available != count * reader.size or count * writer.size > MAX_VECTOR_BYTES:
            raise ValueError
        vectors = []
        for _ in range(count):
            values = reader.unpack(member.read(reader.size))
            if not all(math.isfinite(value) for value in values):
                raise ValueError
            vectors.append(writer.pack(*values))
        if member.read(1):
            raise ValueError
        return column_count, vectors


def parse_vectors(payload: bytes) -> tuple[int, dict[str, bytes]]:
    """解析固定版数值归档；不启用 pickle，不接受越界尺寸或附带文件。"""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            infos = archive.infolist()
            items = {item.filename: item for item in infos}
            if (
                len(infos) != 2
                or items.keys() != {"ids.npy", "vectors.npy"}
                or sum(item.file_size for item in infos) > MAX_VECTOR_BYTES
            ):
                raise ValueError
            keys = _decode_ids(archive, items["ids.npy"])
            width, vectors = _decode_vectors(archive, items["vectors.npy"], len(keys))
            return width, dict(zip(keys, vectors, strict=True))
    except (
        ValueError,
        TypeError,
        SyntaxError,
        RuntimeError,
        OverflowError,
        NotImplementedError,
        RecursionError,
        OSError,
        EOFError,
        struct.error,
        zipfile.BadZipFile,
    ) as error:
        raise MemoryBundleError("向量成员必须是大小受限的有限数值 NPZ。") from error


def prepare_vectors(
    bundle: MemoryBundle,
    records: dict[MemorySource, HistoricalMemory],
    spaces: Mapping[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str, bytes]]]:
    """预检每个向量成员；只有完整指纹、维数和可携带键吻合时才候选安装。"""
    report: list[dict[str, Any]] = []
    rows: list[tuple[str, str, str, bytes]] = []
    targets = semantic_targets(bundle, records)
    for member in sorted(bundle.binary_members.keys() & VECTOR_MEMBERS):
        dimension, values = parse_vectors(bundle.binary_members[member])
        scope = member.removeprefix("vectors/").removesuffix(".npz")
        fingerprint = (
            bundle.manifest.get("image_embedding_fingerprint")
            if scope == "images" and bundle.images is not None
            else bundle.manifest.get("embedding_fingerprint")
        )
        current = spaces.get(scope)
        if (
            not current
            or not isinstance(fingerprint, dict)
            or current != fingerprint
            or current.get("dimension") != dimension
            or (scope != "images" and not current.get("hash"))
        ):
            report.append(
                {
                    "name": member,
                    "status": "skip",
                    "reason": "未配置模型、算法与维数完全相同的向量空间。",
                }
            )
            continue
        if scope == "paragraphs":
            allowed = {
                key: source for (kind, key), source in targets.items() if kind == "paragraph"
            }
        elif scope == "graph":
            allowed = {
                f"{kind}:{key}": source
                for (kind, key), source in targets.items()
                if kind in ("entity", "relation")
            }
        else:
            assets = {source.key[0]: source for source in records if source.table == "image_assets"}
            allowed = {
                row["asset_id"]: assets[row["content_hash"]]
                for row in (bundle.images or {}).get("assets", [])
            }
        if set(values) - allowed.keys():
            report.append(
                {"name": member, "status": "skip", "reason": "向量内容键不完全属于本包语义闭包。"}
            )
            continue
        mapped = {allowed[key].encode(): value for key, value in values.items()}
        if len(mapped) != len(values):
            report.append({"name": member, "status": "skip", "reason": "向量内容键重复映射。"})
            continue
        rows.extend(
            (scope, json.dumps(current, sort_keys=True), key, value)
            for key, value in mapped.items()
        )
        report.append({"name": member, "status": "install", "count": len(values)})
    return report, rows


def open_bundle_vectors(connection: sqlite3.Connection) -> None:
    """向量按包归属；复用的记忆原件不依赖单一包的向量。"""
    connection.execute("""CREATE TABLE IF NOT EXISTS memory_bundle_vectors (
        installation_id TEXT NOT NULL, space TEXT NOT NULL, fingerprint TEXT NOT NULL,
        source_key TEXT NOT NULL, vector BLOB NOT NULL,
        PRIMARY KEY (installation_id, space, source_key)
    ) WITHOUT ROWID""")


def search_vectors(
    connection: sqlite3.Connection,
    scope: str,
    fingerprint: dict[str, Any],
    query: Sequence[float],
    limit: int,
    now: float,
    allowed_sources: Collection[MemorySource] = (),
) -> list[tuple[MemorySource, float]]:
    """只读相同配置空间且仍有效的已安装向量，不调用模型。"""
    if (
        not query
        or len(query) != fingerprint.get("dimension")
        or not all(math.isfinite(value) for value in query)
    ):
        return []
    allowed = {source.encode() for source in allowed_sources}
    query_norm = math.hypot(*query)
    if not query_norm or not math.isfinite(query_norm):
        return []
    decoded: dict[str, float] = {}
    for key, packed in connection.execute(
        """
        SELECT vector.source_key, vector.vector FROM memory_bundle_vectors vector
        JOIN memory_bundles bundle USING (installation_id)
        JOIN memory_history original USING (source_key)
        LEFT JOIN memory_edits edit ON edit.kind='history' AND edit.entry_id=original.id
        WHERE bundle.installed AND vector.space=? AND vector.fingerprint=?
          AND original.status IN ('active', 'missing') AND coalesce(edit.is_enabled, 1)
          AND NOT coalesce(edit.is_removed, 0) AND NOT coalesce(edit.is_bundle_removed, 0)
          AND edit.edited_content IS NULL
          AND (original.valid_from IS NULL OR original.valid_from <= ?)
          AND (original.valid_to IS NULL OR original.valid_to > ?)
        """,
        (scope, json.dumps(fingerprint, sort_keys=True), now, now),
    ):
        if allowed and key not in allowed:
            continue
        values = struct.unpack("<" + "f" * len(query), packed)
        norm = math.hypot(*values)
        if norm:
            distance = sum(
                (left / query_norm) * (right / norm)
                for left, right in zip(query, values, strict=True)
            )
            decoded[key] = max(distance, decoded.get(key, -1.0))
    return [
        (MemorySource.decode(key), value)
        for key, value in sorted(decoded.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]
