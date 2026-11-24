"""合成图片、数值空间及固定版容器代码的双向兼容试验。"""

import ast
import asyncio
import hashlib
import os
import sqlite3
import struct
from contextlib import closing
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from aureus.common import ManualClock
from aureus.memory import HistoricalMemory, MemoryEvidence, MemoryIndex, MemorySource
from aureus.memory.bundle_format import (
    BUNDLE_FORMAT,
    ContentLevel,
    MemoryBundle,
    MemoryBundleError,
    bundle_digest,
    load_bundle,
    paragraph_hash,
    write_bundle,
)
from aureus.memory.bundle_images import canonical_image_semantics
from aureus.memory.bundle_vectors import VECTOR_REBUILD_REASON, parse_vectors
from aureus.timeline import TimelineDatabase

NOW = datetime(2026, 11, 24, tzinfo=UTC)
TEXT = "合成冰川调查记忆"
SPACE = {"model": "synthetic", "algorithm": "test", "dimension": 2, "hash": "synthetic-2"}


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 2), "orange").save(output, "PNG")
    return output.getvalue()


def _npy(descriptor: str, shape: tuple[int, ...], data: bytes) -> bytes:
    header = f"{{'descr': {descriptor!r}, 'fortran_order': False, 'shape': {shape!r}, }}\n".encode()
    return b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header + data


def _npz(key: str, values: tuple[float, float]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("ids.npy", _npy(f"<U{len(key)}", (1,), key.encode("utf-32-le")))
        archive.writestr("vectors.npy", _npy("<f4", (1, 2), struct.pack("<2f", *values)))
    return output.getvalue()


def _make_bundle(package: str, png: bytes, *, target: str | None = None) -> BytesIO:
    digest = hashlib.sha256(png).hexdigest()
    locator = f"images/assets/{digest}.png"
    identifier = paragraph_hash(TEXT)
    knowledge = {
        "format": "lpmm_openie",
        "format_version": 1,
        "docs": [{"id": identifier, "passage": TEXT}],
    }
    images = {
        "assets": [
            {
                "asset_id": "asset",
                "member": locator,
                "content_hash": digest,
                "byte_size": len(png),
                "width": 3,
                "height": 2,
                "mime_type": "image/png",
            }
        ],
        "occurrences": [{"occurrence_id": "occ", "asset_id": "asset", "chat_id": "never-publish"}],
        "observations": [{"observation_id": "obs", "occurrence_id": "occ", "text": "合成冰川图片"}],
        "links": [
            {
                "link_id": "link",
                "occurrence_id": "occ",
                "target_type": "paragraph",
                "target_id": target or identifier,
            }
        ],
    }
    binaries = {locator: png, "vectors/paragraphs.npz": _npz(identifier, (1.0, 0.0))}
    content = bundle_digest(
        {"knowledge": knowledge, "state": None, "images": canonical_image_semantics(images)}
    )
    manifest = {
        "format": BUNDLE_FORMAT,
        "format_version": 2,
        "content_level": "knowledge",
        "package": {"id": package, "version": "1", "name": package},
        "content_digest": content,
        "components": ["knowledge.json", "images.json", *binaries],
        "embedding_fingerprint": SPACE,
    }
    output = BytesIO()
    write_bundle(
        MemoryBundle(
            package, "1", package, "knowledge", content, manifest, knowledge, None, images, binaries
        ),
        output,
    )
    return output


def test_v2_reversible_media_ownership_and_vectors_skipped(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    database = tmp_path / "index.db"
    timeline = TimelineDatabase(database, clock)
    index = MemoryIndex(database, timeline, clock, bundle_vector_spaces={"paragraphs": SPACE})
    timeline.open()
    index.open()
    data = _png()
    digest = hashlib.sha256(data).hexdigest()
    try:
        with pytest.raises(MemoryBundleError, match="语义闭包"):
            index.preview_bundle(_make_bundle("invalid", data, target="foreign"))
        first = _make_bundle("first", data)
        second = _make_bundle("second", data)
        component = index.preview_bundle(first)["components"][-1]
        assert component["status"] == "skip"
        assert "重建" in component["reason"]
        left = index.install_bundle(first, "merge")["installation_id"]
        assert index.install_bundle(first, "merge")["already_installed"]
        right = index.install_bundle(second, "merge")["installation_id"]
        assert index.read_bundle_image(digest) == (data, "image/png")
        assert not index.search_bundle_vectors("paragraphs", (1.0, 0.0), 5)
        assert index.set_bundle_installed(left, False)
        assert index.read_bundle_image(digest) is not None
        assert index.set_bundle_installed(right, False)
        assert index.read_bundle_image(digest) is None
        assert not index.search(["冰川调查"], (), 5)
        assert index.set_bundle_installed(left, True)
        assert index.read_bundle_image(digest) is not None
        original = BytesIO()
        assert index.export_bundle(left, original)
        assert load_bundle(original).images == load_bundle(first).images
    finally:
        index.close()
        timeline.close()


def test_matching_vector_member_skipped_while_knowledge_installs_and_uninstalls(
    tmp_path: Path,
) -> None:
    clock = ManualClock(NOW)
    database = tmp_path / "index.db"
    timeline = TimelineDatabase(database, clock)
    index = MemoryIndex(database, timeline, clock, bundle_vector_spaces={"paragraphs": SPACE})
    timeline.open()
    index.open()
    try:
        installed = index.install_bundle(_make_bundle("vector-skip", _png()), "merge")
        assert installed["status"] == "installed"
        member = [c for c in installed["components"] if c["name"] == "vectors/paragraphs.npz"]
        assert member == [
            {"name": "vectors/paragraphs.npz", "status": "skip", "reason": VECTOR_REBUILD_REASON}
        ]
        with closing(sqlite3.connect(database)) as connection:
            count = connection.execute("SELECT count(*) FROM memory_bundle_vectors").fetchone()
            assert count[0] == 0
        assert not index.search_bundle_vectors("paragraphs", (1.0, 0.0), 5)
        assert index.search(["冰川调查"], (), 5)
        assert index.set_bundle_installed(installed["installation_id"], False)
        assert not index.search(["冰川调查"], (), 5)
    finally:
        index.close()
        timeline.close()


def test_numeric_npz_rejects_nonfinite_and_limits_headers() -> None:
    with pytest.raises(MemoryBundleError):
        parse_vectors(_npz("paragraph", (float("nan"), 0)))
    invalid = BytesIO()
    with ZipFile(invalid, "w") as archive:
        archive.writestr("ids.npy", _npy("<U1", (1,), "a".encode("utf-32-le")))
        archive.writestr("vectors.npy", _npy("|O", (1, 100_000_000), b""))
    with pytest.raises(MemoryBundleError):
        parse_vectors(invalid.getvalue())


def _fixed_service(source: Path) -> tuple[type, dict[str, Any]]:
    original = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    # 保留容器实现原语句，替换本实验不启用的宿主、真实模型和 NumPy 依赖。
    original.body = [
        node
        for node in original.body
        if not (
            (isinstance(node, ast.ImportFrom) and node.level)
            or (isinstance(node, ast.Import) and any(name.name == "numpy" for name in node.names))
        )
    ]
    context: dict[str, Any] = {"KernelServiceBase": object}
    exec(compile(original, str(source), "exec"), context)
    return context["MemoryBundleAdminService"], context


def test_fixed_source_bidirectional_v1_and_v2(tmp_path: Path) -> None:
    configured = os.environ.get("AUREUS_FIXED_BUNDLE_SOURCE")
    if not configured:
        pytest.skip("只在显式指定固定版公开源码时执行互通检查")
    service_type, namespace = _fixed_service(Path(configured))
    data = _png()
    content_hash = hashlib.sha256(data).hexdigest()
    paragraph_id = paragraph_hash(TEXT)

    class Store:
        def read_bytes(self, storage_key: str) -> bytes:
            assert storage_key == "synthetic.png"
            return data

        def list_image_memory_links(self, _items: list[str]) -> list[dict[str, object]]:
            return []

    class Images:
        asset_store = Store()

        def export_records(self, _items: list[str]) -> dict[str, object]:
            return {
                "assets": [
                    {
                        "asset_id": "asset",
                        "storage_key": "synthetic.png",
                        "content_hash": content_hash,
                        "byte_size": len(data),
                        "width": 3,
                        "height": 2,
                        "mime_type": "image/png",
                    }
                ],
                "occurrences": [
                    {"occurrence_id": "occ", "asset_id": "asset", "component_path": "0"}
                ],
                "observations": [],
                "links": [],
                "fingerprint": {},
            }

    class Fixed(service_type):
        metadata_store = Store()

        async def _select_paragraphs(
            self, _selector: object
        ) -> tuple[list[dict[str, str]], dict[str, str]]:
            return [{"hash": paragraph_id, "content": TEXT}], {"type": "all"}

        def _query_by_values(self, *_args: object) -> list[dict[str, str]]:
            return [{"hash": paragraph_id, "content": TEXT}]

        def _collect_related_rows(self, paragraphs: object) -> dict[str, object]:
            return {key: [] for key in namespace["FULL_STATE_TABLE_ORDER"]} | {
                "paragraphs": paragraphs
            }

        def _select_image_occurrence_ids(self, **_kwargs: object) -> list[str]:
            return ["occ"]

        def _image_runtime(self) -> Images:
            return Images()

        def _dual_vector_pools_enabled(self) -> bool:
            return False

        def _bundles_root(self) -> Path:
            return tmp_path

    fixed = Fixed()
    database = tmp_path / "index.db"
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(database, clock)
    index = MemoryIndex(database, timeline, clock, bundle_attachment_root=tmp_path)
    timeline.open()
    index.open()
    try:
        paragraph = MemorySource("synthetic", "memory", "paragraphs", (paragraph_id,))
        asset = MemorySource("synthetic", "memory", "image_assets", ("asset",))
        occurrence = MemorySource("synthetic", "memory", "image_occurrences", ("occ",))
        attachments = tmp_path / "attachments"
        attachments.mkdir()
        (attachments / content_hash).write_bytes(data)
        index.publish(
            [
                HistoricalMemory(paragraph, TEXT),
                HistoricalMemory(
                    asset,
                    "",
                    status="missing",
                    evidence=(
                        MemoryEvidence("attachment_archived", f"attachments/{content_hash}"),
                    ),
                ),
                HistoricalMemory(
                    occurrence,
                    "",
                    status="missing",
                    evidence=(MemoryEvidence("image_assets", "asset", asset),),
                ),
            ],
            snapshot=("synthetic", "fixed-test"),
        )
        [scope] = index.list_bundle_sources()
        combinations: tuple[tuple[ContentLevel, bool], ...] = (
            ("knowledge", False),
            ("full", False),
            ("full", True),
        )
        for level, has_images in combinations:
            upstream = asyncio.run(
                fixed._export_bundle(
                    {
                        "content_level": level,
                        "include_images": has_images,
                        "include_vectors": False,
                        "package": {"name": f"fixed-{level}-{has_images}"},
                    }
                )
            )
            assert upstream["success"]
            with Path(upstream["path"]).open("rb") as inp:
                loaded = index.preview_bundle(inp)
            assert loaded["format_version"] == (2 if has_images else 1)
            assert loaded["content_level"] == level
            preview = index.generate_bundle(
                [scope.source_id], level, None, include_images=has_images
            )
            local = BytesIO()
            index.generate_bundle(
                [scope.source_id],
                level,
                None,
                output=local,
                include_images=has_images,
                expected_digest=preview["preview_digest"],
            )
            local_path = tmp_path / f"aureus-{level}-{has_images}.amembundle"
            local_path.write_bytes(local.getvalue())
            verified = fixed._load_bundle(local_path)
            summary = fixed._public_bundle_summary(verified)
            assert summary["manifest"]["content_digest"] == preview["content_digest"]
            assert summary["content_level"] == level
            assert (
                namespace["_stable_installation_id"](
                    summary["package"]["id"], "1.0.0", "global", "global"
                )
                == preview["installation_id"]
            )
    finally:
        index.close()
        timeline.close()
