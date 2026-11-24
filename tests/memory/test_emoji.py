"""表情资源按内容去重并保留原来源和登记状态。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from aureus.common import ManualClock
from aureus.memory.emoji import EmojiStore, ImportedEmoji
from aureus.memory.history import MemorySource


def _png(color: str) -> bytes:
    with Image.new("RGB", (8, 8), color) as image, BytesIO() as data:
        image.save(data, "PNG")
        return data.getvalue()


def test_capacity_serializes_new_registrations_and_keeps_existing_overage(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    store = EmojiStore(database, tmp_path, clock, registration_limit=2)
    store.open()
    with ThreadPoolExecutor() as workers:
        results = list(
            workers.map(
                lambda color: store.upload(_png(color), registration_description=color),
                ("red", "blue", "green", "yellow"),
            )
        )
    assert len(results) == 4 and all(created for _, created in results)
    assert len(store.candidates()) == store.registration_count() == 2
    smaller = EmojiStore(database, tmp_path, clock, registration_limit=1)
    smaller.open()
    assert smaller.registration_count() == 2
    added, _ = smaller.upload(_png("purple"), registration_description="紫色")
    assert not smaller.is_registered(added)
    assert smaller.registration_count() == 2


def test_replacement_rejects_stale_review_and_keeps_media_on_success(tmp_path: Path) -> None:
    store = EmojiStore(
        tmp_path / "aureus.db",
        tmp_path,
        ManualClock(datetime(2026, 1, 1, tzinfo=UTC)),
        registration_limit=1,
    )
    store.open()
    old_data, new_data = _png("red"), _png("blue")
    old, _ = store.upload(old_data, registration_description="旧表情")
    new, _ = store.upload(new_data, registration_description="新表情")
    [selection] = store.replacement_options(new, 0, 10)
    assert store.review_catalog(new, "rejected")
    with pytest.raises(ValueError):
        store.confirm_replacement(new, old, selection.incoming_version, selection.outgoing_version)
    assert store.review_catalog(new, "approved")
    with pytest.raises(ValueError):
        store.confirm_replacement(new, old, selection.incoming_version, selection.outgoing_version)
    [selection] = store.replacement_options(new, 0, 10)
    store.confirm_replacement(new, old, selection.incoming_version, selection.outgoing_version)
    with pytest.raises(ValueError):
        store.confirm_replacement(new, old, selection.incoming_version, selection.outgoing_version)
    assert store.registration_count() == 1
    assert store.read_media(old) == old_data and store.read_media(new) == new_data
    assert store.read_sendable(old) is None and store.read_sendable(new) == new_data


def test_deduplicates_collections_and_tracks_confirmed_use(tmp_path: Path) -> None:
    store = EmojiStore(
        tmp_path / "aureus.db", tmp_path, ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    )
    store.open()
    data = b"GIF89a" + b"test"
    first = store.collect(data, description="握手", tags=("友好",))
    assert first.digest is not None
    repeated = store.collect(data)
    assert first.digest == repeated.digest
    assert repeated.collection_count == 2
    assert repeated.use_count == 0
    assert not store.candidates()

    store.set_status(first.digest, is_registered=True, is_banned=False)
    assert [row.digest for row in store.candidates()] == [first.digest]
    assert store.read_media(first.digest) == data
    store.record_use(first.digest)
    assert store.browse("友好", 0, 10)[0].use_count == 1

    store.set_status(first.digest, is_registered=True, is_banned=True)
    assert not store.candidates()
    archived = tmp_path / "attachments" / first.digest
    archived.unlink()
    assert store.read_media(first.digest) is None
    assert not store.browse("", 0, 10)[0].has_file


def test_describing_a_collected_emoji_registers_it_without_counting_again(tmp_path: Path) -> None:
    store = EmojiStore(
        tmp_path / "aureus.db", tmp_path, ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    )
    store.open()
    image = b"GIF89a synthetic media"
    entry = store.collect(image)
    assert entry.digest is not None
    assert not entry.is_registered

    registered = store.collect(image, description="合成描述", tags=("高兴",), register=True)
    assert registered.is_registered and registered.collection_count == 1
    assert registered.tags == ("高兴",)
    store.set_status(entry.digest, is_registered=False, is_banned=False)
    confirmed_again = store.collect(image, description="忽略这条描述", register=True)
    assert not confirmed_again.is_registered and confirmed_again.collection_count == 1
    assert confirmed_again.description == "合成描述"

    other = b"GIF89a synthetic disabled media"
    disabled = store.collect(other)
    assert disabled.digest is not None
    store.set_status(disabled.digest, is_registered=False, is_banned=True)
    confirmed = store.collect(other, description="已禁用", register=True)
    assert confirmed.is_banned and not confirmed.is_registered


def test_import_keeps_source_status_and_counts_when_repeated(tmp_path: Path) -> None:
    store = EmojiStore(
        tmp_path / "aureus.db", tmp_path, ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    )
    store.open()
    digest = "a" * 64
    imported = ImportedEmoji(
        MemorySource("fixture", "main", "images", (17,)),
        digest,
        "旧描述",
        True,
        False,
        1,
        12,
        0,
        None,
        "{}",
    )

    assert store.import_records([imported]) == (1, 1)
    first = store.browse("旧描述", 0, 10)[0]
    assert first.source == imported.source.encode()
    assert first.is_registered and not first.has_file
    assert first.use_count == 12

    store.set_status(digest, is_registered=False, is_banned=True)
    assert store.import_records([imported]) == (0, 0)
    latest = store.browse("", 0, 10)[0]
    assert latest.is_banned and not latest.is_registered
    assert latest.use_count == 12


@pytest.mark.parametrize("change", ["ban", "remove", "description", "tags"])
def test_automatic_emoji_registration_preserves_intervening_maintenance(
    tmp_path: Path, change: str
) -> None:
    store = EmojiStore(
        tmp_path / "aureus.db", tmp_path, ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    )
    store.open()
    image = b"GIF89a synthetic media"
    entry = store.collect(image)
    assert entry.digest is not None
    if change == "ban":
        store.set_status(entry.digest, is_registered=False, is_banned=True)
    elif change == "remove":
        assert store.set_removed([entry.entry_key], True)
    elif change == "description":
        assert store.update_catalog(entry.digest, description="人工描述")
    else:
        assert store.update_catalog(entry.digest, tags=("人工标签",))

    assert not store.register_recognition(entry.digest, ("惊讶",))
    [preserved] = store.browse_page("", 0, 10, removed=change == "remove")[0]
    assert not preserved.is_registered and preserved.collection_count == 1
    assert preserved.description == ("人工描述" if change == "description" else "")
    assert preserved.tags == (("人工标签",) if change == "tags" else ())
    assert preserved.is_banned == (change == "ban")
    assert preserved.is_removed == (change == "remove")
    assert store.read_media(entry.digest) == image
