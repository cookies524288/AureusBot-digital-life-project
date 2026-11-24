"""统一语言库的证据、排序与整条裁剪。"""

from pathlib import Path

from aureus.memory import LanguageKind, LanguageProposal, LanguageSample, LanguageStore
from aureus.memory.language_matching import (
    LanguageEntry,
    TermFrequency,
    format_language,
    match_jargons,
    select_expressions,
)


def test_jargon_match_keeps_senses_and_uses_frequency_and_position() -> None:
    entries = [
        LanguageEntry("XP", "本局胜负", "", 20, (31,), frozenset()),
        LanguageEntry("GO", "向前走", "", 4, (32,), frozenset()),
        LanguageEntry("gO", "现在开始", "", 4, (33,), frozenset()),
        LanguageEntry("GO", "向前走", "", 3, (34,), frozenset()),
        LanguageEntry("now", "此刻", "", 20, (35,), frozenset()),
        LanguageEntry("absent", "并未命中", "", 99, (36,), frozenset()),
    ]
    matches = match_jargons(
        ["<message id='1'> NOW", "xp", "gO"], entries, {"go": TermFrequency(2, 4)}
    )
    assert [match.evidence_ids for match in matches] == [(32, 34), (33,), (35,), (31,)]
    assert match_jargons(["<message tag='XP'> 空白"], entries, {}) == []
    assert format_language(matches, jargon=True, budget=10) == ""
    first_only = format_language([entries[0]], jargon=True, budget=700)
    block = format_language([entries[0], entries[1]], jargon=True, budget=len(first_only))
    assert block == first_only
    assert block.endswith("XP：本局胜负")
    assert "GO" not in block


def test_expression_topic_speaker_avoid_and_limit() -> None:
    entries = [
        LanguageEntry(f"说法{index}", "庆祝跑步", "", index + 1, (index,), frozenset({str(index)}))
        for index in range(5)
    ]
    hits = select_expressions(entries, "刚跑步回来", "庆祝", ("说法4",), "0")
    assert [entry.text for entry in hits] == ["说法0", "说法3", "说法2"]
    assert select_expressions(entries, "问候", "", (), "0") == []


def test_observing_existing_expression_keeps_example_and_counts_unique_messages(
    tmp_path: Path,
) -> None:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    initial = LanguageSample("timeline:1", 1, "person:1", "跑步时说月兔冲线")
    later = LanguageSample("from-channel", 2, "person:2", "又说月兔冲线")
    store.observe(
        [initial], [LanguageProposal(LanguageKind.EXPRESSION, "月兔冲线", "庆祝跑步", (1, 99))]
    )
    store.observe([later], [])
    store.observe([LanguageSample("from-channel", 3, "person:2", later.content)], [])
    [entry] = store.entries(LanguageKind.EXPRESSION)
    assert entry.detail == "庆祝跑步"
    assert entry.example == initial.content
    assert entry.count == 2
    assert entry.evidence_ids == (1, 2)
    assert entry.speakers == {"person:1", "person:2"}


def test_presented_evidence_must_be_a_prefix_of_the_original(tmp_path: Path) -> None:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    sample = LanguageSample("timeline:51", 51, "person:1", "真实用语，另有参考")
    store.observe(
        [sample],
        [LanguageProposal(LanguageKind.EXPRESSION, "伪造词", "合成情景", (51,))],
        presented_content={51: "伪造词"},
    )
    assert not store.entries(LanguageKind.EXPRESSION)


def test_jargon_stays_out_of_retrieval_until_evidence_exists(tmp_path: Path) -> None:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    for number in range(1, 4):
        sample = LanguageSample(f"timeline:{number}", number, "person:1", "小队集合 GO")
        store.observe(
            [sample], [LanguageProposal(LanguageKind.JARGON, "Go", "集合完成", (number,))]
        )
        assert not store.jargon_reference(["GO"], 700)
    sample = LanguageSample("timeline:4", 4, "person:1", "集合，go")
    store.observe([sample], [LanguageProposal(LanguageKind.JARGON, "go", "集合完成", (4,))])
    assert "Go：集合完成" in store.jargon_reference(["GO"], 700)
