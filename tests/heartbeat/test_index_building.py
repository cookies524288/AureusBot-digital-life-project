"""心跳后台自动建索引：假器官、手动时钟，不联网（D125）。"""

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, IndexingConfig, LanguageConfig, ManualClock
from aureus.common.sqlite import open_connection
from aureus.core.embedding_progress import EmbeddingProgress
from aureus.core.model_runtime import EmbeddingSpaces, ModelRuntime
from aureus.heartbeat import (
    Heartbeat,
    MediaReference,
    MessageRoute,
    Perception,
    TrustLevel,
    VisibilityScope,
)
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.identity import Identity
from aureus.memory import (
    EmojiStore,
    ExpressionVectorIndex,
    LanguageContent,
    LanguageKind,
    LanguageStore,
)
from aureus.memory.language import LanguagePackageRow
from aureus.organs import (
    LOCAL_EXPRESSION_EMBEDDING,
    KeyResolver,
    OrganBus,
    OrganCallError,
    OrganRequest,
    OrganResponse,
)
from aureus.safety import (
    BudgetExceededError,
    EmergencyStop,
    EnergyDepletedError,
    PermissionChecker,
)
from aureus.timeline import TimelineDatabase

START = datetime(2026, 11, 25, tzinfo=UTC)
SPACE = "synthetic-expression-space"
TEXT_SPACE = "synthetic-text-space"
IMAGE_SPACE = "synthetic-image-space"
PNG = b"\x89PNG\r\n\x1a\n"


def make_identity() -> Identity:
    return Identity.model_validate(
        {
            "facts": {"name": "小绿", "birthday": "2008-01-01"},
            "user": {"name": "阿明"},
            "persona": {"immutable": "温和。"},
        }
    )


class FakeEmbeddings:
    """按器官返回合成向量；可以拒绝、也可以在某一批之前停下等测试放行。"""

    def __init__(self) -> None:
        self.requests: list[OrganRequest] = []
        self.reject: Exception | None = None
        self.expression_space = SPACE
        self.text_space = TEXT_SPACE
        self.image_space = IMAGE_SPACE
        self.gate_at: int | None = None
        self.gate = asyncio.Event()
        self.reached = asyncio.Event()

    async def call(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        if self.gate_at is not None and len(self.requests) >= self.gate_at:
            self.reached.set()
            await self.gate.wait()
        if self.reject is not None:
            raise self.reject
        inputs = request.embedding_inputs
        if request.organ_id == LOCAL_EXPRESSION_EMBEDDING:
            space = request.embedding_space or self.expression_space
            return OrganResponse(
                request.organ_id,
                "",
                metadata={"embedding_space": space},
                embeddings=tuple((1.0, float(index)) for index in range(len(inputs))),
            )
        if request.organ_id == "text_embedding":
            return OrganResponse(
                request.organ_id,
                "",
                metadata={"embedding_space": self.text_space},
                embeddings=tuple((1.0, 0.0) for _ in inputs),
            )
        if request.organ_id == "image_embedding":
            return OrganResponse(
                request.organ_id,
                "",
                metadata={"embedding_space": self.image_space},
                embeddings=((1.0, 0.0),) * len(inputs),
            )
        raise AssertionError(f"没有为器官 {request.organ_id} 准备合成向量")


class Harness:
    """一个只装了需要部分的真心跳；不接通道，只驱动后台构建。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        expressions: int = 0,
        language: bool = False,
        emoji: int = 0,
        inbox: bool = False,
        indexing: IndexingConfig | None = None,
    ) -> None:
        self.clock = ManualClock(START)
        self.database = tmp_path / "aureus.db"
        self.timeline = TimelineDatabase(self.database, self.clock)
        self.timeline.open()
        self.call = FakeEmbeddings()
        self.language: LanguageStore | None = None
        self.expression_index: ExpressionVectorIndex | None = None
        if expressions or language:
            self.language = LanguageStore(self.database)
            self.language.open()
            for number in range(expressions):
                self.language.add_entry(
                    LanguageKind.EXPRESSION,
                    LanguageContent(f"说法{number:02}", f"场景{number:02}"),
                )
            self.expression_index = ExpressionVectorIndex(self.database)
        self.emoji_store: EmojiStore | None = None
        if emoji:
            self.emoji_store = EmojiStore(self.database, tmp_path / "media", self.clock)
            self.emoji_store.open()
            for number in range(emoji):
                self.emoji_store.collect(
                    PNG + bytes([number]), description=f"表情{number:02}", register=True
                )
        self.inbox = InboxStore(self.database, self.clock) if inbox else None
        self.stop = EmergencyStop(tmp_path / "halt.json", self.clock)
        self.heartbeat = Heartbeat(
            clock=self.clock,
            timeline=self.timeline,
            organs=self.call,
            identity=make_identity(),
            config=HeartbeatConfig(
                qq_emoji_maintenance_enabled=False, split_reply_enabled=False
            ),
            indexing=indexing
            or IndexingConfig(batch_size=2, interval_seconds=0),
            permissions=PermissionChecker(self.stop),
            emergency_stop=self.stop,
            timezone=UTC,
            language=self.language,
            language_config=LanguageConfig(),
            expression_index=self.expression_index,
            emoji_store=self.emoji_store,
            inbox_store=self.inbox,
        )
        self.heartbeat.wake_up()

    def coverage(self, space: str = SPACE) -> tuple[int, int]:
        assert self.language is not None and self.expression_index is not None
        return self.expression_index.coverage(self.language, space, frozenset())

    async def beat_and_wait(self) -> None:
        self.heartbeat.beat()
        task = self.heartbeat._indexing_task
        if task is not None:
            await task


def _runtime(tmp_path: Path) -> ModelRuntime:
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=False
    )
    runtime.embedding_spaces = EmbeddingSpaces(
        expression=SPACE, text=((1, TEXT_SPACE),), image=((1, IMAGE_SPACE),)
    )
    return runtime


@pytest.mark.asyncio
async def test_auto_builder_starts_when_embedding_available(tmp_path: Path) -> None:
    """文本嵌入路线可用时自动按批建到完成，进度从部分变为完整（验收 1、6）。"""
    harness = Harness(tmp_path, expressions=5)
    harness.call.gate_at = 2
    harness.heartbeat.update_available_organs(frozenset(), expression_space=SPACE)
    harness.heartbeat.beat()
    task = harness.heartbeat._indexing_task
    assert task is not None
    await asyncio.wait_for(harness.call.reached.wait(), timeout=5)
    progress = EmbeddingProgress(
        _runtime(tmp_path),
        expression_enabled=True,
        language=harness.language,
        expression_index=harness.expression_index,
    )
    partial = await progress.read("text_embedding")
    assert partial is not None
    assert (
        partial[0].kind,
        partial[0].state,
        partial[0].established,
        partial[0].eligible,
    ) == ("expression", "partial", 2, 5)
    harness.call.gate.set()
    await task
    complete = await progress.read("text_embedding")
    assert complete is not None
    assert (complete[0].state, complete[0].established, complete[0].eligible) == (
        "complete",
        5,
        5,
    )
    assert [len(request.embedding_inputs) for request in harness.call.requests] == [2, 2, 1]


@pytest.mark.asyncio
async def test_auto_builder_skips_revoked_sources(tmp_path: Path) -> None:
    """来源撤权后不再为该会话补建；已建部分保留，统计如实反映缺项（验收 3）。"""
    harness = Harness(tmp_path, language=True, inbox=True)
    assert harness.language is not None and harness.inbox is not None
    scope = ConversationKey("qq", "bot", "group", "group-1").language_scope_key()

    def add_package(text: str) -> None:
        assert harness.language is not None
        package = LanguagePackageRow(LanguageContent(text, "测试"), 1, "来源", text)
        harness.language.save_package(LanguageKind.EXPRESSION, [package])
        harness.language.package_scope(
            LanguageKind.EXPRESSION, f"package:{package.digest}", [scope], confirm=True
        )

    add_package("限范围表达一")
    harness.inbox.add(
        Perception(
            "qq",
            "member",
            "你好",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", "group-1", "m1"),
        )
    )
    harness.heartbeat.set_inbox_route_filter(lambda key: key.recipient_id == "group-1")
    harness.heartbeat.update_available_organs(frozenset(), expression_space=SPACE)
    await harness.beat_and_wait()
    assert harness.expression_index is not None
    assert harness.expression_index.coverage(
        harness.language, SPACE, frozenset({scope})
    ) == (1, 1)

    add_package("限范围表达二")
    harness.heartbeat.set_inbox_route_filter(lambda _key: False)
    before = len(harness.call.requests)
    await harness.beat_and_wait()
    assert len(harness.call.requests) == before
    assert harness.expression_index.coverage(
        harness.language, SPACE, frozenset({scope})
    ) == (1, 2)


@pytest.mark.asyncio
async def test_emoji_cache_prebuild_reaches_complete(tmp_path: Path) -> None:
    """文本嵌入可用时按批预建表情描述缓存，直到覆盖全部可发送表情（验收 1、6）。"""
    harness = Harness(tmp_path, emoji=3)
    harness.call.gate_at = 2
    harness.heartbeat.update_available_organs(
        frozenset({"text_embedding"}), text_spaces=((1, TEXT_SPACE),)
    )
    harness.heartbeat.beat()
    task = harness.heartbeat._indexing_task
    assert task is not None
    await asyncio.wait_for(harness.call.reached.wait(), timeout=5)
    selector = harness.heartbeat.emoji_selector
    assert selector is not None
    progress = EmbeddingProgress(_runtime(tmp_path), emoji_selector=selector)
    partial = await progress.read("text_embedding")
    assert partial is not None
    [emoji] = [item for item in partial if item.kind == "emoji_cache"]
    assert (emoji.state, emoji.established, emoji.eligible) == ("partial", 2, 3)
    harness.call.gate.set()
    await task
    assert await selector.cache_coverage(((1, TEXT_SPACE),)) == [(1, 3, 3)]
    assert [len(request.embedding_inputs) for request in harness.call.requests] == [2, 1]


@pytest.mark.asyncio
async def test_auto_builder_rebuilds_after_embedding_space_change(tmp_path: Path) -> None:
    """模型换了空间后丢弃旧空间结果，从新空间重建（行为细则 5）。"""
    harness = Harness(tmp_path, expressions=3, emoji=2)
    harness.heartbeat.update_available_organs(
        frozenset({"text_embedding"}),
        expression_space=SPACE,
        text_spaces=((1, TEXT_SPACE),),
    )
    await harness.beat_and_wait()
    assert harness.coverage() == (3, 3)
    selector = harness.heartbeat.emoji_selector
    assert selector is not None
    assert await selector.cache_coverage(((1, TEXT_SPACE),)) == [(1, 2, 2)]

    harness.call.expression_space = "new-expression-space"
    harness.call.text_space = "new-text-space"
    harness.heartbeat.update_available_organs(
        frozenset({"text_embedding"}),
        expression_space="new-expression-space",
        text_spaces=((1, "new-text-space"),),
    )
    await harness.beat_and_wait()
    assert harness.coverage("new-expression-space") == (3, 3)
    assert await selector.cache_coverage(((1, "new-text-space"),)) == [(1, 2, 2)]
    assert await selector.cache_coverage(((1, TEXT_SPACE),)) == [(1, 0, 2)]


@pytest.mark.asyncio
async def test_image_backfill_indexes_existing_unindexed_images(tmp_path: Path) -> None:
    """图片嵌入可用时回填已收到但未建索引的图片，只处理当前获准会话（验收 2、6）。"""
    harness = Harness(tmp_path, inbox=True)
    assert harness.inbox is not None
    harness.call.gate_at = 2

    def message(group: str, name: str, *files: str) -> Perception:
        return Perception(
            "qq",
            "bot",
            "图片",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", group, name),
            attachments=tuple(MediaReference("image", file_id) for file_id in files),
        )

    harness.inbox.add(message("group-1", "m1", "file-1", "file-2"))
    harness.inbox.add(message("group-2", "m2", "file-3"))
    images = InboxImageIndex(harness.database, harness.call.call, lambda: True)

    async def read(perception: Perception, attachment: MediaReference) -> tuple[bytes, str]:
        return attachment.file_id.encode(), "image/png"

    harness.heartbeat.set_inbox_route_filter(lambda key: key.recipient_id == "group-1")
    harness.heartbeat.set_inbox_attachment_reader(read)
    harness.heartbeat.set_inbox_image_indexer(images)
    harness.heartbeat.beat()
    task = harness.heartbeat._indexing_task
    assert task is not None
    await asyncio.wait_for(harness.call.reached.wait(), timeout=5)
    progress = EmbeddingProgress(
        _runtime(tmp_path),
        images=images,
        allows_conversation=lambda key: key.recipient_id == "group-1",
    )
    partial = await progress.read("image_embedding")
    assert partial is not None
    assert (partial[0].state, partial[0].established, partial[0].eligible) == (
        "partial",
        1,
        2,
    )
    harness.call.gate.set()
    await task
    complete = await progress.read("image_embedding")
    assert complete is not None
    assert (complete[0].state, complete[0].established, complete[0].eligible) == (
        "complete",
        2,
        2,
    )

    with closing(open_connection(harness.database)) as connection:
        rows = connection.execute(
            "SELECT attachment FROM inbox_image_vectors ORDER BY attachment"
        ).fetchall()
    assert [row[0] for row in rows] == ["file-1", "file-2"]
    assert [request.organ_id for request in harness.call.requests] == [
        "image_embedding",
        "image_embedding",
    ]


@pytest.mark.asyncio
async def test_image_backfill_stops_after_circuit_skip(tmp_path: Path) -> None:
    """服务商熔断时本批只尝试一次就停下，返回还有缺项，不抛到心跳（D150）。"""
    harness = Harness(tmp_path, inbox=True)
    assert harness.inbox is not None
    harness.call.reject = OrganCallError(
        "image_embedding", ["服务商熔断冷却中"], skipped_by_circuit=True
    )

    harness.inbox.add(
        Perception(
            "qq",
            "bot",
            "图片",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", "group-1", "m1"),
            attachments=tuple(
                MediaReference("image", file_id) for file_id in ("file-1", "file-2", "file-3")
            ),
        )
    )
    images = InboxImageIndex(harness.database, harness.call.call, lambda: True)

    async def read(perception: Perception, attachment: MediaReference) -> tuple[bytes, str]:
        return attachment.file_id.encode(), "image/png"

    more, written = await images.backfill_batch(harness.inbox, read, lambda _key: True, 5)

    assert (more, written) == (True, False)
    assert [request.organ_id for request in harness.call.requests] == ["image_embedding"]
    with closing(open_connection(harness.database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbox_image_vectors").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_auto_builder_stops_on_halt_and_model_removal(tmp_path: Path) -> None:
    """急停和模型移除立即停下，已建批次保留，不报错（验收 3）。"""
    halted = Harness(tmp_path / "halted", expressions=4)
    halted.call.gate_at = 2
    halted.heartbeat.update_available_organs(frozenset(), expression_space=SPACE)
    halted.heartbeat.beat()
    task = halted.heartbeat._indexing_task
    assert task is not None
    await asyncio.wait_for(halted.call.reached.wait(), timeout=5)
    assert halted.coverage() == (2, 4)
    halted.heartbeat.halt("测试急停")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert halted.coverage() == (2, 4)
    halted.heartbeat.beat()
    assert halted.heartbeat._indexing_task is None
    assert len(halted.call.requests) == 2

    removed = Harness(tmp_path / "removed", expressions=4)
    removed.call.gate_at = 2
    removed.heartbeat.update_available_organs(frozenset(), expression_space=SPACE)
    removed.heartbeat.beat()
    task = removed.heartbeat._indexing_task
    assert task is not None
    await asyncio.wait_for(removed.call.reached.wait(), timeout=5)
    removed.heartbeat.update_available_organs(frozenset(), expression_space=None)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed.coverage() == (2, 4)
    removed.heartbeat.beat()
    assert removed.heartbeat._indexing_task is None
    assert len(removed.call.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejection",
    [
        BudgetExceededError(LOCAL_EXPRESSION_EMBEDDING, "本月额度用完"),
        EnergyDepletedError(LOCAL_EXPRESSION_EMBEDDING, "今天可选额度用完"),
    ],
)
async def test_auto_builder_respects_budget_and_energy(
    tmp_path: Path, rejection: Exception
) -> None:
    """网关拒绝时暂停；条件恢复后从原处继续（验收 4）。"""
    harness = Harness(tmp_path, expressions=3)
    harness.call.reject = rejection
    harness.heartbeat.update_available_organs(frozenset(), expression_space=SPACE)
    await harness.beat_and_wait()
    assert harness.coverage() == (0, 3)
    assert harness.heartbeat._indexing_retry_at is not None

    harness.call.reject = None
    harness.clock.advance(timedelta(seconds=300))
    await harness.beat_and_wait()
    assert harness.coverage() == (3, 3)


@pytest.mark.asyncio
async def test_auto_build_disabled_falls_back_to_on_demand(tmp_path: Path) -> None:
    """关掉总开关后不启动后台任务，回复后的按需补建照常工作（验收 5）。"""
    harness = Harness(
        tmp_path,
        expressions=3,
        indexing=IndexingConfig(auto_build_enabled=False, batch_size=2, interval_seconds=0),
    )
    harness.heartbeat.update_available_organs(frozenset(), expression_space=SPACE)
    harness.heartbeat.beat()
    assert harness.heartbeat._indexing_task is None
    assert harness.call.requests == []

    recall = harness.heartbeat.expression_recall
    assert recall is not None
    recall.backfill(None)
    task = recall._backfill
    assert task is not None
    await task
    assert harness.coverage() == (3, 3)
