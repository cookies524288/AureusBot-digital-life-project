"""合成平台原件只供平台读取，长期记忆保留事实及非正文定位。"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from aureus.common import ManualClock, MemoryConfig
from aureus.memory import HistoricalMemory, MemoryEntry, MemoryEvidence, MemoryIndex, MemorySource
from aureus.memory.consolidation import ConsolidationBatch, ConsolidationItem
from aureus.memory.imported_messages import ImportedPlatformHistory
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope

NOW = datetime(2026, 11, 24, tzinfo=UTC)


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[tuple[Path, TimelineDatabase, MemoryIndex]]:
    path = tmp_path / "memory.db"
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    config = MemoryConfig(consolidation_enabled=True, consolidation_extract_enabled=True)
    index = MemoryIndex(path, timeline, clock, config=config)
    index.open()
    try:
        yield path, timeline, index
    finally:
        index.close()
        timeline.close()


def test_platform_originals_do_not_enter_memory_catalog_search_or_exports(
    memory: tuple[Path, TimelineDatabase, MemoryIndex],
) -> None:
    path, timeline, index = memory
    old = timeline.append("message", "旧入站合成原句", ActorType.USER)
    new = timeline.append("message", "新外发合成原句", ActorType.ME)
    experience = timeline.append("diary_written", "整理后的日记", ActorType.ME)
    assert index.index_more(10, lambda row: MemoryEntry("同名", row.content)) == 3
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("SELECT record_id FROM memory_index").fetchall() == [(experience.id,)]
        conn.execute(
            "INSERT INTO memory_index(rowid, record_id, content, actor_label, recorded_at) "
            "VALUES (?, ?, ?, '同名', ?)",
            (old.id, old.id, old.content, old.timestamp.isoformat()),
        )

    imported = [
        HistoricalMemory(MemorySource(account, "main", table, (1,)), text, "同名")
        for account, table, text in (
            ("account-a", "mai_messages", "迁入甲的合成原句"),
            ("account-b", "messages", "迁入乙的合成原句"),
        )
    ]
    paragraph = HistoricalMemory(
        MemorySource("account-a", "memory", "paragraphs", (1,)),
        "提炼后的稳定事实",
        evidence=(MemoryEvidence("message", "1", imported[0].source),),
    )
    assert index.publish([*imported, paragraph]) == 3
    for keyword in ("合成", "合成原句", *(record.text for record in imported)):
        assert not index.search([keyword], (), 10, since=NOW)
    assert [hit.record_id for hit in index.browse_live("", 0, 10)] == [experience.id]
    assert index.browse_history("", "", 0, 10, source_table="mai_messages") == []
    assert index.count_history_sources() == {"paragraphs": 1}
    _, total, counts = index.browse_text_resources("", "", "", "", 0, 10)
    assert (total, counts) == (2, {"experiences": 1, "paragraphs": 1})
    [hit] = index.search(["稳定事实"], (), 10)
    assert hit.source == paragraph.source and hit.evidence == paragraph.evidence
    assert index.get_history(imported[0].source) is None
    assert not index.update_resource("live", old.id, "合成覆盖", True)
    assert not index.set_resources_removed([("live", old.id)], True)
    assert timeline.get(old.id) == old and timeline.get(new.id) == new
    platform = ImportedPlatformHistory(path).page(before_id=None, limit=10)
    assert [(row.source, row.text) for row in platform] == [
        (row.source, row.text) for row in reversed(imported)
    ]

    with closing(sqlite3.connect(path)) as conn:
        assert not conn.execute(
            "SELECT rowid FROM memory_history_fts WHERE memory_history_fts MATCH '合成原句'"
        ).fetchall()
    ndjson, knowledge = BytesIO(), BytesIO()
    index.export_archive(ndjson)
    index.export_knowledge(knowledge)
    with ZipFile(knowledge) as bundle:
        exports = (ndjson.getvalue(), bundle.read("knowledge.json"))
    assert all(paragraph.text.encode() in data for data in exports)
    assert all("合成原句".encode() not in data for data in exports)


def test_legacy_extracted_terms_return_only_fact_and_evidence_pointer(
    memory: tuple[Path, TimelineDatabase, MemoryIndex],
) -> None:
    path, timeline, index = memory
    original = timeline.append(
        "message",
        "合成私聊原句：我每周观察土星。",
        ActorType.USER,
        visibility=VisibilityScope.USER_ONLY,
        metadata={"person_id": 42},
    )
    reference = f"timeline:{original.id}"
    item = ConsolidationItem(reference, ("timeline",), original.content, NOW.timestamp(), True)
    assert index.index_more(10, lambda row: MemoryEntry("用户", row.content)) == 1
    assert index.consolidation_batch(1) is None
    assert index.save_consolidation(ConsolidationBatch((0, 0), (0, original.id), (item,)), [])
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("SELECT count(*) FROM memory_candidates").fetchone() == (0,)
        for identity, kind, text in (
            ("experience", "experience", original.content),
            ("copy", "fact", "合成私聊原句"),
            ("derived", "preference", "偏好规律天文观测"),
        ):
            conn.execute(
                "INSERT INTO memory_candidates VALUES (?, ?, ?, ?)",
                (identity, kind, text, NOW.timestamp()),
            )
            conn.execute(
                "INSERT INTO memory_candidate_evidence VALUES (?, ?)", (identity, reference)
            )
    assert index.search(["合成私聊原句"], (), 10) == []
    assert index.search(["天文观测"], (), 10, include_user_extractions=False) == []
    assert index.search(["天文观测"], (), 10, person_key="aureus:other") == []
    assert index.search(["天文观测"], [original.id], 10) == []
    [hit] = index.search(["天文观测"], (), 10, person_key="aureus:42")
    assert hit.record_id is None and hit.text == "偏好规律天文观测"
    assert hit.evidence == (MemoryEvidence("platform_message", reference),)
    [candidate] = index.consolidated_candidates(10)
    assert candidate.text == hit.text and candidate.evidence == (reference,)
