"""把包内图像映射到记忆原件，不读取或恢复聊天定位。"""

import hashlib
from typing import Any

from aureus.memory.bundle_format import (
    MemoryBundle,
    MemoryBundleError,
    bundle_digest,
    paragraph_hash,
)
from aureus.memory.history import HistoricalMemory, MemoryEvidence, MemorySource


def semantic_targets(
    bundle: MemoryBundle, records: dict[MemorySource, HistoricalMemory]
) -> dict[tuple[str, str], MemorySource]:
    """根据实际投影建立跨容器版本的可携带资源键映射。"""
    types = {
        "paragraphs": "paragraph",
        "entities": "entity",
        "relations": "relation",
        "episodes": "episode",
    }
    targets = {
        (types[source.table], str(source.key[0])): source
        for source in records
        if source.table in types and records[source].status in ("active", "missing")
    }
    if bundle.state is None:
        for (kind, key), source in list(targets.items()):
            if kind == "entity":
                targets[kind, hashlib.sha256(key.encode()).hexdigest()] = source
        for doc in bundle.knowledge["docs"]:
            paragraph = targets.get(("paragraph", paragraph_hash(doc["passage"])))
            if paragraph is None:
                continue
            for triple in doc.get("extracted_triples", []):
                relation = targets.get(("relation", bundle_digest(tuple(triple))))
                if relation is not None:
                    targets["relation", hashlib.sha256(str(tuple(triple)).encode()).hexdigest()] = (
                        relation
                    )
    return targets


def project_images(bundle: MemoryBundle, records: dict[MemorySource, HistoricalMemory]) -> None:
    """核验链接都在本包语义闭包内，去除消息、来源路径和外部标识。"""
    if bundle.images is None:
        return
    targets = semantic_targets(bundle, records)
    images = bundle.images
    assets: dict[str, MemorySource] = {}
    occurrences: dict[str, MemorySource] = {}
    for row in images.get("assets", []):
        digest = row["content_hash"]
        source = MemorySource("a-memorix-bundle", "bundle-images", "image_assets", (digest,))
        assets[row["asset_id"]] = source
        records[source] = HistoricalMemory(
            source,
            "已安装包图片",
            evidence=(
                MemoryEvidence("attachment_archived", f"attachments/{digest}"),
                MemoryEvidence("media_type", row["mime_type"]),
            ),
        )
    for row in images.get("occurrences", []):
        if row.get("status", "active") != "active":
            raise MemoryBundleError("图片出现不属于有效的图片闭包。")
        source = _image_source(bundle, "image_occurrences", row, "occurrence_id")
        occurrences[row["occurrence_id"]] = source
        asset = assets[row["asset_id"]]
        records[source] = HistoricalMemory(
            source,
            "",
            status="missing",
            evidence=(MemoryEvidence("image_asset", str(asset.key[0]), asset),),
        )
    for row in images.get("observations", []):
        if row.get("superseded_by"):
            continue
        source = _image_source(bundle, "image_observations", row, "observation_id")
        occurrence = occurrences[row["occurrence_id"]]
        records[source] = HistoricalMemory(
            source,
            row["text"],
            evidence=(MemoryEvidence("image_occurrence", str(occurrence.key[0]), occurrence),),
        )
    for row in images.get("links", []):
        target = targets.get((row["target_type"], row["target_id"]))
        if target is None:
            raise MemoryBundleError("图片链接指向未在本包语义闭包内的目标。")
        if row.get("status", "active") != "active":
            continue
        source = _image_source(bundle, "image_memory_links", row, "link_id")
        occurrence = occurrences[row["occurrence_id"]]
        records[source] = HistoricalMemory(
            source,
            "",
            status="missing",
            evidence=(
                MemoryEvidence("image_occurrence", str(occurrence.key[0]), occurrence),
                MemoryEvidence(row["target_type"], str(target.key[0]), target),
            ),
        )


def _image_source(bundle: MemoryBundle, table: str, row: dict[str, Any], key: str) -> MemorySource:
    return MemorySource("a-memorix-bundle", bundle.digest, table, (row[key],))
