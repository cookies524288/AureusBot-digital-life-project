"""验证后台索引只重试可安全重跑的模型失败。"""

import asyncio
import sqlite3
from typing import NoReturn

import pytest

from aureus.heartbeat import background_tasks
from aureus.heartbeat.background_tasks import IndexKind, IndexSupervisor
from aureus.organs import OrganCallError
from aureus.safety import LedgerUnavailableError, PermissionDeniedError

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("kind", ["expression", "emoji_cache", "images"])
async def test_index_restarts_three_times_then_raises_the_original_failure(
    kind: IndexKind, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    delays: list[float] = []
    errors = [OrganCallError("embedding", [f"模型失败 {attempt}"]) for attempt in range(4)]
    raised: list[OrganCallError] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def build() -> NoReturn:
        error = errors[len(raised)]
        raised.append(error)
        raise error

    monkeypatch.setattr(background_tasks, "sleep", sleep)
    with pytest.raises(OrganCallError) as failure:
        await IndexSupervisor().run_batch(kind, build)

    assert failure.value is errors[-1]
    assert raised == errors
    assert delays == [1, 5, 30]
    assert [record.exc_info[1] for record in caplog.records if record.exc_info] == errors
    assert all(kind in record.message for record in caplog.records)


async def test_index_restart_counts_are_independent_and_success_does_not_reset_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    failing = True

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def build() -> tuple[bool, bool]:
        nonlocal failing
        failing = not failing
        if not failing:
            raise OrganCallError("embedding", ["模型暂时失败"])
        return True, True

    monkeypatch.setattr(background_tasks, "sleep", sleep)
    supervisor = IndexSupervisor()
    assert await supervisor.run_batch("expression", build) == (True, True)
    assert await supervisor.run_batch("images", build) == (True, True)
    assert await supervisor.run_batch("expression", build) == (True, True)

    assert delays == [1, 1, 5]


@pytest.mark.parametrize(
    "error",
    [
        PermissionDeniedError("embedding", "权限拒绝"),
        LedgerUnavailableError("embedding", "账本不可写"),
        sqlite3.OperationalError("库写入失败"),
        ValueError("状态错误"),
        OrganCallError("embedding", ["服务商冷却"], skipped_by_circuit=True),
        asyncio.CancelledError(),
    ],
)
async def test_index_does_not_retry_state_errors_circuit_skips_or_cancellation(
    error: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def sleep(_delay: float) -> NoReturn:
        pytest.fail("不应为这类异常安排重试")

    async def build() -> NoReturn:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(background_tasks, "sleep", sleep)
    with pytest.raises(type(error)) as failure:
        await IndexSupervisor().run_batch("images", build)
    assert failure.value is error
    assert calls == 1
