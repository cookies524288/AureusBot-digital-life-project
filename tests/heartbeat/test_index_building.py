"""心跳后台自动建索引：假器官、手动时钟，不联网（D125）。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, IndexingConfig, LanguageConfig, ManualClock
from aureus.core.embedding_progress import EmbeddingProgress
from aureus.core.model_runtime import EmbeddingSpaces, ModelRuntime
from aureus.heartbeat import Heartbeat
from aureus.identity import Identity
from aureus.memory import (
    ExpressionVectorIndex,
    LanguageContent,
    LanguageKind,
    LanguageStore,
)
from aureus.organs import (
    LOCAL_EXPRESSION_EMBEDDING,
    KeyResolver,
    OrganBus,
    OrganRequest,
    OrganResponse,
)
from aureus.safety import CallRejectedError, EmergencyStop, PermissionChecker
from aureus.timeline import TimelineDatabase

START = datetime(2026, 11, 25, tzinfo=UTC)
SPACE = "synthetic-expression-space"
TEXT_SPACE = "synthetic-text-space"
IMAGE_SPACE = "synthetic-image-space"


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
        self.gate_at: int | None = None
        self.gate = asyncio.Event()
        self.gate.set()
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
            assert request.embedding_space == SPACE
            return OrganResponse(
                request.organ_id,
                "",
                metadata={"embedding_space": SPACE},
                embeddings=tuple((1.0, float(index)) for index in range(len(inputs))),
            )
        if request.organ_id == "text_embedding":
            return OrganResponse(
                request.organ_id,
                "",
                metadata={"embedding_space": TEXT_SPACE},
                embeddings=tuple((1.0, 0.0) for _ in inputs),
            )
        if request.organ_id == "image_embedding":
            return OrganResponse(
                request.organ_id,
                "",
                metadata={"embedding_space": IMAGE_SPACE},
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
        indexing: IndexingConfig | None = None,
    ) -> None:
        self.clock = ManualClock(START)
        self.database = tmp_path / "aureus.db"
        self.timeline = TimelineDatabase(self.database, self.clock)
        self.timeline.open()
        self.call = FakeEmbeddings()
        self.language: LanguageStore | None = None
        self.expression_index: ExpressionVectorIndex | None = None
        if expressions:
            self.language = LanguageStore(self.database)
            self.language.open()
            for number in range(expressions):
                self.language.add_entry(
                    LanguageKind.EXPRESSION,
                    LanguageContent(f"说法{number:02}", f"场景{number:02}"),
                )
            self.expression_index = ExpressionVectorIndex(self.database)
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
        )
        self.heartbeat.wake_up()

    def coverage(self) -> tuple[int, int]:
        assert self.language is not None and self.expression_index is not None
        return self.expression_index.coverage(self.language, SPACE, frozenset())

    async def beat_and_wait(self) -> None:
        self.heartbeat.beat()
        task = self.heartbeat._indexing_task
        if task is not None:
            await task


def _runtime(tmp_path: Path) -> ModelRuntime:
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=False
    )
    runtime.embedding_spaces = EmbeddingSpaces(expression=SPACE)
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
async def test_auto_builder_respects_budget_and_energy(tmp_path: Path) -> None:
    """网关拒绝时暂停；条件恢复后从原处继续（验收 4）。"""
    harness = Harness(tmp_path, expressions=3)
    harness.call.reject = CallRejectedError(LOCAL_EXPRESSION_EMBEDDING, "合成拒绝")
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
