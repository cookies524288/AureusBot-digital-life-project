"""用假时钟和收件箱验证总结协议、证据边界及原子提交。"""

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock, MemoryConfig
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.memory_consolidation import MemoryConsolidator, summary_material
from aureus.heartbeat.memory_summary_source import PlatformSummarySource
from aureus.heartbeat.perception import MediaReference, MessageRoute, Perception, TrustLevel
from aureus.memory import MemoryIndex, SummaryScope, SummaryWindow, parse_delta
from aureus.organs import ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.timeline import TimelineDatabase, VisibilityScope

_EMPTY = '{"summary":"","entities":[],"relations":[],"facts":[]}'
_SCOPE = VisibilityScope.GROUP
_PREFERENCE = ModelPreference.CHEAPEST


@dataclass
class Rig:
    path: Path
    inbox: InboxStore
    index: MemoryIndex
    source: PlatformSummarySource
    runner: MemoryConsolidator
    key: ConversationKey

    @property
    def scope(self) -> SummaryScope:
        return self.key.language_scope_key(), _SCOPE

    def add(
        self,
        message_id: str,
        content: str,
        *,
        sender: str = "200",
        name: str = "阿青",
        account: str = "100",
        visibility: VisibilityScope = _SCOPE,
        image: bool = False,
        outgoing: bool = False,
        quote: str | None = None,
    ) -> None:
        perception = Perception(
            "qq",
            sender,
            content,
            visibility,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute(account, "group", "300", message_id),
            quoted_message_id=quote,
            attachments=(MediaReference("image", "asset"),) if image else (),
        )
        if outgoing:
            self.inbox.add_delivered(perception, sender_name=name)
        else:
            assert self.inbox.add(perception, sender_name=name, conversation_name="同名会话")

    def window(self) -> SummaryWindow:
        window = self.source.next_window(self.runner.config.consolidation_batch)
        assert window is not None
        return window


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    path = tmp_path / "isolated.db"
    clock = ManualClock(datetime(2026, 11, 24, tzinfo=UTC))
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    inbox = InboxStore(path, clock)
    config = MemoryConfig(
        consolidation_enabled=True, consolidation_extract_enabled=True, consolidation_batch=2
    )
    index = MemoryIndex(path, timeline, clock, config=config)
    index.open()
    source = PlatformSummarySource(inbox, index)
    key = ConversationKey("qq", "100", "group", "300")
    source.allows = lambda candidate: candidate == key
    index.summary_evidence = source.check_evidence
    runner = MemoryConsolidator(index, config)
    runner.source, runner.has_organ = source, True
    try:
        yield Rig(path, inbox, index, source, runner, key)
    finally:
        index.close()
        timeline.close()


def _answer(
    value: str,
    excerpt: str,
    *,
    message: str = "m1",
    sender: str = "200",
    image: tuple[str, str] | None = None,
    replaces: int | None = None,
    subject: str = "阿青",
) -> str:
    text = f"阿青：居住地点是{value}。"
    return json.dumps(
        {
            "summary": text,
            "entities": ["阿青", value],
            "relations": [
                {
                    "subject": subject,
                    "predicate": "居住地点",
                    "object": value,
                }
            ],
            "facts": [
                {
                    "fact_id": "f1",
                    "text": text,
                    "predicate": "居住地点",
                    "value": value,
                    "source": {"message_id": message, "sender_id": sender, "excerpt": excerpt},
                    "image_evidence": (
                        [{"message_id": image[0], "component_path": image[1]}] if image else []
                    ),
                    "replaces": replaces,
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_only_explicit_empty_advances_and_cancel_does_not_consume_window(rig: Rig) -> None:
    rig.add("m1", "我住在晴桂巷。")
    rig.add("m2", "合成上下文")
    attempts = iter([OrganCallError("summarizer", ["离线失败"]), "[]", "{", _EMPTY])
    entered = asyncio.Event()

    async def interrupted(request: OrganRequest) -> OrganResponse:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("等待会被取消")

    task = asyncio.create_task(rig.runner.run(interrupted, _PREFERENCE))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def answer(request: OrganRequest) -> OrganResponse:
        assert (
            request.organ_id == "summarizer" and request.context["task"] == "memory_consolidation"
        )
        value = next(attempts)
        if isinstance(value, OrganCallError):
            raise value
        return OrganResponse(request.organ_id, value)

    for _ in range(3):
        assert not (await rig.runner.run(answer, _PREFERENCE)).completed
        assert rig.index.summary_reference(rig.scope) == (0, (), ())
    assert (await rig.runner.run(answer, _PREFERENCE)).completed
    assert rig.index.summary_reference(rig.scope) == (2, (), ())


@pytest.mark.asyncio
async def test_same_window_once_and_image_not_interchangeable(rig: Rig) -> None:
    rig.add("outside", "我住在未授权的地点。", account="999", image=True)
    rig.add("m1", "我目前住在晴桂巷。", image=True)
    rig.add("m2", "我是阿青青。", sender="201", name="阿青青", image=True)
    rig.add("narrower", "我住在私下地点。", visibility=VisibilityScope.USER_ONLY)
    window = rig.window()
    materials = json.loads(summary_material(window, "模拟助手"))
    assert [entry["message_id"] for entry in materials["messages"]] == ["m1", "m2"]
    assert materials["image_evidence_catalog"] == [
        {"message_id": "m1", "component_path": "0"},
        {"message_id": "m2", "component_path": "0"},
    ]
    for image in [("m2", "0"), ("outside", "0"), ("m1", "1")]:
        with pytest.raises(ValueError):
            parse_delta(_answer("晴桂巷", "我目前住在晴桂巷。", image=image), window)
    with pytest.raises(ValueError):
        parse_delta(_answer("晴桂巷", "我目前住在晴桂巷。", subject="未出现的人"), window)
    delta = parse_delta(_answer("晴桂巷", "我目前住在晴桂巷。", image=("m1", "0")), window)

    async def commit() -> bool:
        return await asyncio.to_thread(
            rig.index.commit_summary, window, delta, rig.source.check_window
        )

    assert sorted(await asyncio.gather(commit(), commit())) == [False, True]
    assert not await commit()
    assert rig.index.search(["晴桂巷"], (), 4) == []
    hits = rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope)
    assert len(hits) == 1 and hits[0].text == "阿青：居住地点是晴桂巷。"
    assert [(item.kind, item.reference) for item in hits[0].evidence] == [
        ("platform_message", "m1"),
        ("platform_image", "0"),
    ]
    assert rig.index.search(["我目前住在晴桂巷"], (), 4, summary_scope=rig.scope) == []
    with sqlite3.connect(rig.path) as connection:
        assert connection.execute("SELECT count(*) FROM memory_summary_deltas").fetchone() == (1,)
        connection.execute(
            "DELETE FROM inbox_messages WHERE message_id IN ('m1', 'm2', 'narrower')"
        )
    assert rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope) == []
    assert rig.index.summary_reference(rig.scope) == (3, (), ())
    rig.add("m3", "我目前住在晴桂巷。")
    rig.add("m4", "其他合成上下文")
    assert rig.window().messages[0].sequence == 5


@pytest.mark.parametrize(
    ("excerpt", "outgoing", "quote"),
    [
        ("我目前住在晴桂巷。", True, None),
        ("> 我目前住在晴桂巷。", False, "old"),
        ("我听说他现在住在晴桂巷。", False, None),
        ("我可能住在晴桂巷。", False, None),
        ("我给个示例：晴桂巷。", False, None),
        ("我只是玩笑，晴桂巷。", False, None),
    ],
)
def test_context_cannot_be_self_evidence(
    rig: Rig,
    excerpt: str,
    outgoing: bool,
    quote: str | None,
) -> None:
    rig.add("m1", excerpt, outgoing=outgoing, quote=quote, image=True)
    rig.add("m2", "我确认另一件事情。")
    window = rig.window()
    assert not window.messages[0].can_be_evidence
    with pytest.raises(ValueError):
        parse_delta(_answer("晴桂巷", excerpt, image=("m1", "0")), window)
    assert rig.index.summary_reference(rig.scope) == (0, (), ())


def test_correction_retains_history_and_rechecks_route_under_transaction(rig: Rig) -> None:
    original = "我一直住在晴桂巷。"
    rig.add("m1", original)
    rig.add("m2", "第二条原件")
    first = rig.window()
    delta = parse_delta(_answer("晴桂巷", original), first)
    assert rig.index.commit_summary(first, delta, rig.source.check_window)
    corrected = "我更正一下，现在住在栖松巷。"
    rig.add("m3", corrected)
    rig.add("m4", original)
    window = rig.window()
    assert window.previous_summaries == ("阿青：居住地点是晴桂巷。",)
    old = window.previous_facts[0]
    with pytest.raises(ValueError):
        parse_delta(_answer("晴桂巷", original, message="m4"), window)
    with pytest.raises(ValueError):
        parse_delta(_answer("栖松巷", corrected, message="m3", replaces=9999), window)
    with pytest.raises(ValueError):
        parse_delta(_answer("栖松巷", corrected, message="m3"), window)
    delta = parse_delta(_answer("栖松巷", corrected, message="m3", replaces=old.identifier), window)
    rig.source.allows = lambda _: False
    assert not rig.index.commit_summary(window, delta, rig.source.check_window)
    assert rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope) == []
    rig.source.allows = lambda key: key == rig.key
    assert rig.index.commit_summary(window, delta, rig.source.check_window)
    assert len(rig.index.search(["栖松巷"], (), 4, summary_scope=rig.scope)) == 1
    assert rig.index.search(["晴桂巷"], (), 4, summary_scope=rig.scope) == []
    assert rig.index.summary_reference(rig.scope)[1] == ("阿青：居住地点是栖松巷。",)
    with sqlite3.connect(rig.path) as connection:
        rows = connection.execute("SELECT id, superseded_by FROM memory_summary_facts ORDER BY id")
        assert rows.fetchall() == [(old.identifier, old.identifier + 1), (old.identifier + 1, None)]


@pytest.mark.asyncio
async def test_failed_cursor_write_rolls_back_accepted_facts(rig: Rig) -> None:
    excerpt = "我目前住在晴桂巷。"
    rig.add("m1", excerpt)
    rig.add("m2", "配套聊天上下文")

    async def answer(request: OrganRequest) -> OrganResponse:
        assert excerpt in request.prompt
        return OrganResponse(request.organ_id, _answer("晴桂巷", excerpt))

    with sqlite3.connect(rig.path) as connection:
        connection.execute("""CREATE TRIGGER synthetic_write_error
            BEFORE INSERT ON memory_summary_cursors BEGIN
                SELECT RAISE(ABORT, 'synthetic rollback');
            END""")
    assert not (await rig.runner.run(answer, _PREFERENCE)).completed
    assert rig.index.summary_reference(rig.scope) == (0, (), ())
    with sqlite3.connect(rig.path) as connection:
        assert connection.execute("SELECT count(*) FROM memory_summary_facts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM memory_summary_deltas").fetchone() == (0,)
        connection.execute("DROP TRIGGER synthetic_write_error")
    assert (await rig.runner.run(answer, _PREFERENCE)).completed
    assert rig.index.summary_reference(rig.scope)[0] == 2
