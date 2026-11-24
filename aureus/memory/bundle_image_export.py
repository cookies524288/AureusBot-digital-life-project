"""从明确选中的迁入资产归档生成 v2 图片成员。"""

import hashlib
from pathlib import Path
from typing import Any

from aureus.memory.bundle_closure import BundleClosure
from aureus.memory.bundle_images import CONTENT_HASH, IMAGE_LIMIT_BYTES, inspect_image
from aureus.memory.history import HistoricalMemory, MemorySource


def _local_asset(record: HistoricalMemory, archive: Path | None) -> tuple[str, bytes] | None:
    if archive is None:
        return None
    for link in record.evidence:
        digest = link.reference.removeprefix("attachments/")
        if (
            link.kind != "attachment_archived"
            or link.reference == digest
            or not CONTENT_HASH.fullmatch(digest)
        ):
            continue
        root = (archive / "attachments").resolve()
        target = (root / digest).resolve()
        if root.parent != archive.resolve() or target.parent != root:
            return None
        try:
            with target.open("rb") as reader:
                data = reader.read(IMAGE_LIMIT_BYTES + 1)
        except OSError:
            return None
        if len(data) <= IMAGE_LIMIT_BYTES and hashlib.sha256(data).hexdigest() == digest:
            return digest, data
    return None


def _reference(record: HistoricalMemory, table: str) -> MemorySource | None:
    candidates = {
        link.source
        for link in record.evidence
        if link.source
        and link.source.table == table
        and (link.source.instance, link.source.role) == (record.source.instance, record.source.role)
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _id(source: MemorySource) -> str:
    return hashlib.sha256(source.encode().encode()).hexdigest()


def export_images(
    records: dict[str, HistoricalMemory],
    closure: BundleClosure,
    archive: Path | None,
    *,
    is_full: bool,
) -> tuple[dict[str, Any] | None, dict[str, bytes]]:
    """只带有获准原件的图片，不把聊天定位写进包。"""
    images: dict[str, Any] = {
        "assets": [],
        "occurrences": [],
        "observations": [],
        "links": [],
        "fingerprint": {},
    }
    members: dict[str, bytes] = {}
    assets: dict[str, dict[str, Any]] = {}
    occurrences: set[str] = set()
    for record in records.values():
        if record.source.table != "image_occurrences":
            continue
        source = _reference(record, "image_assets")
        asset = records.get(source.encode()) if source else None
        found = _local_asset(asset, archive) if asset else None
        if not found:
            closure.skip("图片出现没有获准且可校验的原件")
            continue
        digest, data = found
        if digest not in assets:
            try:
                mime, extensions, width, height = inspect_image(data)
            except ValueError:
                closure.skip("图片原件格式不支持")
                continue
            member = f"images/assets/{digest}.{min(extensions, key=lambda item: (len(item), item))}"
            assets[digest] = {
                "asset_id": digest,
                "content_hash": digest,
                "member": member,
                "mime_type": mime,
                "byte_size": len(data),
                "width": width,
                "height": height,
            }
            members[member] = data
        occurrence = _id(record.source)
        occurrences.add(occurrence)
        images["occurrences"].append(
            {
                "occurrence_id": occurrence,
                "asset_id": digest,
                "source_kind": "bundle",
                "component_path": "",
                "status": "active",
            }
        )
    _add_image_links(images, closure, records, occurrences, is_full)
    images["assets"] = [assets[key] for key in sorted(assets)]
    return (images, members) if assets else (None, {})


def _add_image_links(
    images: dict[str, Any],
    closure: BundleClosure,
    records: dict[str, HistoricalMemory],
    occurrences: set[str],
    is_full: bool,
) -> None:
    table_names = {"paragraphs": "paragraph", "entities": "entity", "relations": "relation"}
    if is_full:
        table_names["episodes"] = "episode"
    for record in records.values():
        if record.source.table not in ("image_observations", "image_memory_links"):
            continue
        source = _reference(record, "image_occurrences")
        if source is None or (occurrence := _id(source)) not in occurrences:
            closure.skip("图片观察或链接不在获准图片闭包")
            continue
        if record.source.table == "image_observations":
            if record.status == "active" and record.text:
                images["observations"].append(
                    {
                        "observation_id": _id(record.source),
                        "occurrence_id": occurrence,
                        "text": record.text,
                        "source_kind": "imported",
                        "confirm_status": "confirmed",
                        "evidence_json": "{}",
                    }
                )
            continue
        candidates = [
            link.source
            for link in record.evidence
            if link.source
            and link.source.table in table_names
            and link.source.instance == source.instance
        ]
        if len(candidates) != 1 or candidates[0].encode() not in closure.keys:
            closure.skip("图片链接目标不在所选语义闭包")
            continue
        target = candidates[0]
        images["links"].append(
            {
                "link_id": _id(record.source),
                "occurrence_id": occurrence,
                "target_type": table_names[target.table],
                "target_id": closure.keys[target.encode()],
                "link_kind": "related",
                "status": "active",
                "evidence_json": "{}",
            }
        )
