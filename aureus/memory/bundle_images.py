"""固定版图片语义、闭包校验及事务内内容地址归属。"""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

IMAGE_MEMBER = re.compile(r"images/assets/([0-9a-f]{64})\.([a-z0-9]{1,8})")
CONTENT_HASH = re.compile(r"[0-9a-f]{64}")
IMAGE_LIMIT_PIXELS = 24_000_000
IMAGE_LIMIT_BYTES = 32 * 1024 * 1024
_IMAGE_FORMATS = {
    "PNG": ("image/png", {"png"}),
    "JPEG": ("image/jpeg", {"jpg", "jpeg"}),
    "GIF": ("image/gif", {"gif"}),
    "WEBP": ("image/webp", {"webp"}),
    "BMP": ("image/bmp", {"bmp"}),
    "TIFF": ("image/tiff", {"tif", "tiff"}),
}
_LABELS = {
    "assets": "asset_id",
    "occurrences": "occurrence_id",
    "observations": "observation_id",
    "links": "link_id",
}
_NON_SEMANTIC_EVIDENCE = {"chat_id", "external_ref", "imported", "message_id", "source_link_id"}


def _evidence(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return (
        {key: item for key, item in value.items() if key not in _NON_SEMANTIC_EVIDENCE}
        if isinstance(value, dict)
        else {}
    )


def canonical_image_semantics(images: dict[str, Any]) -> dict[str, Any]:
    """按 062d45c 容器协议计算图片内容语义，排除实例位置与运行状态。"""
    assets = {row["asset_id"]: row for row in images.get("assets", [])}
    occurrences = sorted(
        images.get("occurrences", []),
        key=lambda row: (
            assets[row["asset_id"]]["content_hash"],
            row.get("component_path") or "",
            row["occurrence_id"],
        ),
    )
    occurrence_keys = {
        row["occurrence_id"]: f"occurrence-{index}" for index, row in enumerate(occurrences)
    }
    return {
        "assets": sorted(
            [
                {
                    field: row[field]
                    for field in ("byte_size", "content_hash", "height", "mime_type", "width")
                }
                for row in assets.values()
            ],
            key=lambda row: row["content_hash"],
        ),
        "occurrences": [
            {
                "asset_hash": assets[row["asset_id"]]["content_hash"],
                "component_path": row.get("component_path") or "",
            }
            for row in occurrences
        ],
        "observations": sorted(
            [
                {
                    "confirm_status": row.get("confirm_status") or "",
                    "occurrence": occurrence_keys[row["occurrence_id"]],
                    "source_kind": row.get("source_kind") or "",
                    "text": row["text"],
                    "evidence": _evidence(row.get("evidence_json")),
                }
                for row in images.get("observations", [])
            ],
            key=lambda row: (row["occurrence"], row["source_kind"], row["text"]),
        ),
        "links": sorted(
            [
                {
                    "link_kind": row.get("link_kind") or "",
                    "occurrence": occurrence_keys[row["occurrence_id"]],
                    "target_id": row["target_id"],
                    "target_type": row["target_type"],
                    "evidence": _evidence(row.get("evidence_json")),
                }
                for row in images.get("links", [])
            ],
            key=lambda row: (row["occurrence"], row["target_type"], row["target_id"]),
        ),
    }


def inspect_image(data: bytes) -> tuple[str, set[str], int, int]:
    """只检查支持的位图格式、尺寸和编码，不读外部文件。"""
    if len(data) > IMAGE_LIMIT_BYTES:
        raise ValueError("图片成员字节过多。")
    try:
        with Image.open(BytesIO(data), formats=list(_IMAGE_FORMATS)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > IMAGE_LIMIT_PIXELS:
                raise ValueError("图片成员尺寸过大或无效。")
            mime, suffixes = _IMAGE_FORMATS[image.format or ""]
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("图片成员编码无效。") from error
    return mime, suffixes, width, height


def validate_images(images: dict[str, Any], members: dict[str, bytes]) -> None:
    """验图片成员及内部外键；所有检查在安装事务以前完成。"""
    identities: dict[str, set[str]] = defaultdict(set)
    for table, column in _LABELS.items():
        rows = images.get(table, [])
        if not isinstance(rows, list):
            raise ValueError("图片记录必须是数组。")
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get(column), str)
                or not row[column].strip()
            ):
                raise ValueError("图片记录缺少有效标识。")
            if row[column] in identities[table]:
                raise ValueError("图片记录标识重复。")
            identities[table].add(row[column])
            for field in (
                "text",
                "source_kind",
                "confirm_status",
                "component_path",
                "target_id",
                "target_type",
                "link_kind",
            ):
                if field in row and not isinstance(row[field], str):
                    raise ValueError("图片记录字段类型无效。")
    consumed: set[str] = set()
    hashes: set[str] = set()
    for asset in images.get("assets", []):
        member = asset.get("member")
        found = IMAGE_MEMBER.fullmatch(member) if isinstance(member, str) else None
        if not found or member not in members or member in consumed:
            raise ValueError("图片资产成员缺失、路径无效或重复。")
        data = members[member]
        digest, suffix = found.groups()
        if (
            digest in hashes
            or asset.get("content_hash") != digest
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError("图片资产内容摘要不一致或重复。")
        mime, suffixes, width, height = inspect_image(data)
        if any(type(asset.get(field)) is not int for field in ("byte_size", "width", "height")):
            raise ValueError("图片资产尺寸和大小必须为整数。")
        if suffix not in suffixes or (mime, len(data), width, height) != tuple(
            asset.get(field) for field in ("mime_type", "byte_size", "width", "height")
        ):
            raise ValueError("图片资产格式、尺寸或大小与声明不一致。")
        hashes.add(digest)
        consumed.add(member)
    if consumed != {key for key in members if IMAGE_MEMBER.fullmatch(key)}:
        raise ValueError("记忆包含未声明的图片原件。")
    seen_assets = set()
    for row in images.get("occurrences", []):
        asset_id = row.get("asset_id")
        if not isinstance(asset_id, str) or asset_id not in identities["assets"]:
            raise ValueError("图片出现缺少所属资产。")
        seen_assets.add(asset_id)
    if seen_assets != identities["assets"]:
        raise ValueError("完整图片资产缺少出现记录。")
    for table in ("observations", "links"):
        for row in images.get(table, []):
            occurrence = row.get("occurrence_id")
            if not isinstance(occurrence, str) or occurrence not in identities["occurrences"]:
                raise ValueError("图片观察或链接不属于包的出现记录。")
            required = ("text",) if table == "observations" else ("target_id", "target_type")
            if any(not row.get(field, "").strip() for field in required):
                raise ValueError("图片观察或链接缺少必要内容。")


def open_bundle_images(connection: sqlite3.Connection) -> None:
    """图片字节与包安装记录使用同一个事务，不写外部目录。"""
    connection.execute("""CREATE TABLE IF NOT EXISTS memory_bundle_images (
        digest TEXT PRIMARY KEY, mime TEXT NOT NULL, content BLOB NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS memory_bundle_image_owners (
        installation_id TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (installation_id, digest)
    ) WITHOUT ROWID""")


def store_images(
    connection: sqlite3.Connection,
    installation_id: str,
    images: dict[str, Any],
    members: dict[str, bytes],
) -> None:
    """复用相同摘要的原件；不影响其他包的归属。"""
    for asset in images.get("assets", []):
        digest = asset["content_hash"]
        connection.execute(
            "INSERT OR IGNORE INTO memory_bundle_images VALUES (?,?,?)",
            (digest, asset["mime_type"], members[asset["member"]]),
        )
        connection.execute(
            "INSERT INTO memory_bundle_image_owners VALUES (?,?)", (installation_id, digest)
        )


def read_bundle_image(connection: sqlite3.Connection, digest: str) -> tuple[bytes, str] | None:
    """仅返回仍有安装所有者并且当前资产没有被移除的图片。"""
    if not CONTENT_HASH.fullmatch(digest):
        return None
    row = connection.execute(
        """
        SELECT asset.content, asset.mime FROM memory_bundle_images asset
        WHERE asset.digest = ? AND EXISTS (
            SELECT 1 FROM memory_bundle_image_owners owner
            JOIN memory_bundles bundle USING (installation_id)
            JOIN memory_bundle_members member USING (installation_id)
            JOIN memory_history history ON history.id = member.entry_id
            LEFT JOIN memory_edits edit ON edit.kind = 'history' AND edit.entry_id = history.id
            WHERE owner.digest = asset.digest AND bundle.installed
              AND json_extract(history.source_key, '$[2]') = 'image_assets'
              AND json_extract(history.source_key, '$[3][0]') = asset.digest
              AND coalesce(edit.is_enabled, 1) AND NOT coalesce(edit.is_removed, 0)
              AND NOT coalesce(edit.is_bundle_removed, 0)
        )""",
        (digest,),
    ).fetchone()
    return (row[0], row[1]) if row else None
