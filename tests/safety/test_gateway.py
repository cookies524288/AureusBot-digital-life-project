"""安全网关的测试：0.8 的验收标准（docs/08-roadmap.md）。

- 超限、失控、读不了账本时拒绝调用；
- 失败和估算的用量都进账本；
- 拒绝和失败都记进时间线（只有自己可见）。
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureus.common import BudgetCalendar, ManualClock, SafetyConfig
from aureus.organs import (
    CallAttempt,
    OrganBus,
    OrganCallCancelled,
    OrganCallError,
    OrganRequest,
    OrganResponse,
)
from aureus.safety import (
    EVENT_CALL_FAILED,
    EVENT_CALL_REJECTED,
    BudgetExceededError,
    CallRejectedError,
    EmergencyStop,
    Ledger,
    LedgerUnavailableError,
    PermissionChecker,
    PermissionDeniedError,
    RunawayBlockedError,
    SafetyGateway,
)
from aureus.safety.gateway import MODEL_TEST_PERMITS
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

pytestmark = pytest.mark.asyncio

START = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
ORGAN = "replyer"


def make_attempt(prompt: int = 100, completion: int = 50, **overrides: Any) -> CallAttempt:
    fields: dict[str, Any] = {
        "model": "model-a",
        "provider": "provider-a",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }
    fields.update(overrides)
    return CallAttempt(**fields)


class ScriptedOrgan:
    """按剧本返回结果的器官：每次调用取剧本里的下一项，是异常就抛出，是响应就返回。"""

    def __init__(
        self,
        organ_id: str,
        script: list[OrganResponse | Exception],
        during_call: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.organ_id = organ_id
        self.calls = 0
        self._script = script
        self._during_call = during_call

    async def process(self, request: OrganRequest) -> OrganResponse:
        self.calls += 1
        if self._during_call is not None:
            await self._during_call()
        outcome = self._script[min(self.calls, len(self._script)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def ok(*attempts: CallAttempt, organ_id: str = ORGAN) -> OrganResponse:
    return OrganResponse(organ_id=organ_id, suggestion="好的", attempts=list(attempts))


def all_models_failed(*attempts: CallAttempt, organ_id: str = ORGAN) -> OrganCallError:
    return OrganCallError(organ_id, ["model-a：超时"], attempts)


@dataclass
class Harness:
    clock: ManualClock
    bus: OrganBus
    ledger: Ledger
    timeline: TimelineDatabase
    db_path: Path
    emergency_stop: EmergencyStop

    def build(self, config: SafetyConfig | None = None) -> SafetyGateway:
        return SafetyGateway(
            self.bus,
            self.ledger,
            self.timeline,
            self.clock,
            config or SafetyConfig(),
            PermissionChecker(self.emergency_stop),
        )

    def add_organ(
        self,
        script: list[OrganResponse | Exception],
        organ_id: str = ORGAN,
        during_call: Callable[[], Awaitable[None]] | None = None,
    ) -> ScriptedOrgan:
        organ = ScriptedOrgan(organ_id, script, during_call)
        self.bus.register(organ)
        return organ

    def request(self, organ_id: str = ORGAN) -> OrganRequest:
        return OrganRequest(organ_id=organ_id, prompt="你好")

    def events(self, event_type: str) -> list[TimelineRecord]:
        return list(self.timeline.query(TimelineQuery(event_type=event_type)))


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    clock = ManualClock(START)
    db_path = tmp_path / "aureus.db"
    ledger = Ledger(db_path, clock, calendar=BudgetCalendar(UTC))
    timeline = TimelineDatabase(db_path, clock)
    ledger.open()
    timeline.open()
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    yield Harness(clock, OrganBus(), ledger, timeline, db_path, stop)
    ledger.close()
    timeline.close()


async def test_interrupted_stream_records_usage_without_a_failure(harness: Harness) -> None:
    class InterruptedOrgan:
        organ_id = ORGAN

        async def process(self, request: OrganRequest) -> OrganResponse:
            assert request.stream
            raise OrganCallCancelled((make_attempt(10, 12, estimated=True, succeeded=False),))

    harness.bus.register(InterruptedOrgan())
    request = OrganRequest(ORGAN, "规划材料", stream=True)
    with pytest.raises(asyncio.CancelledError):
        await harness.build().call(request)
    assert harness.ledger.tokens_used_in_month(harness.clock.now()) == 22
    assert harness.events(EVENT_CALL_FAILED) == []


async def test_circuit_breaker_does_not_trip_runaway_guard(harness: Harness) -> None:
    skipped = OrganCallError(ORGAN, ["正在冷却"], skipped_by_circuit=True)
    harness.add_organ([skipped, skipped, skipped, skipped, ok(make_attempt(10, 2))])
    gateway = harness.build(SafetyConfig(max_consecutive_failures=2))
    for _ in range(4):
        with pytest.raises(OrganCallError, match="正在冷却"):
            await gateway.call(harness.request())
    assert harness.events(EVENT_CALL_FAILED) == []
    assert harness.ledger.tokens_used_in_month(harness.clock.now()) == 0
    assert (await gateway.call(harness.request())).suggestion == "好的"
    assert harness.ledger.tokens_used_in_month(harness.clock.now()) == 12


def drop_ledger_table(db_path: Path) -> None:
    """从数据库文件外面把账本表删掉，模拟账本坏了。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE ledger")
        conn.commit()
    finally:
        conn.close()


# ---------- 正常放行和记账 ----------


async def test_allowed_call_returns_the_response_and_records_every_attempt(
    harness: Harness,
) -> None:
    harness.add_organ(
        [
            ok(
                make_attempt(10, 0, succeeded=False, failure_reason="认证失败"),
                make_attempt(200, 40, cached_tokens=150),
            )
        ]
    )
    gateway = harness.build()
    response = await gateway.call(harness.request())
    assert response.suggestion == "好的"
    entries = harness.ledger.entries()
    assert [(entry.succeeded, entry.prompt_tokens) for entry in entries] == [
        (False, 10),
        (True, 200),
    ]
    assert all(entry.organ_id == ORGAN for entry in entries)


async def test_organ_without_model_usage_leaves_the_ledger_empty(harness: Harness) -> None:
    harness.add_organ([ok()])  # 比如假器官：没有调用模型
    await harness.build().call(harness.request())
    assert harness.ledger.entries() == []


async def test_successful_call_writes_nothing_to_the_timeline(harness: Harness) -> None:
    harness.add_organ([ok(make_attempt())])
    await harness.build().call(harness.request())
    assert harness.timeline.count_records() == 0


async def test_unregistered_organ_is_a_value_error_and_not_counted(harness: Harness) -> None:
    gateway = harness.build(SafetyConfig(max_calls_per_minute=1))
    with pytest.raises(ValueError, match="未注册"):
        await gateway.call(harness.request("typo"))
    harness.add_organ([ok()])
    await gateway.call(harness.request())  # 前面写错名字的那次没有占频率名额
    assert harness.timeline.count_records() == 0


# ---------- 月度预算 ----------


async def test_call_is_rejected_once_the_monthly_limit_is_reached(harness: Harness) -> None:
    organ = harness.add_organ([ok(make_attempt(100, 50))])
    gateway = harness.build(SafetyConfig(monthly_token_limit=150))
    await gateway.call(harness.request())  # 用了 150，刚好到上限
    with pytest.raises(BudgetExceededError):
        await gateway.call(harness.request())
    assert organ.calls == 1  # 被拒绝的调用没有真的发给器官


async def test_call_below_the_limit_is_allowed_and_may_overshoot_it(harness: Harness) -> None:
    harness.add_organ([ok(make_attempt(100, 50))])
    gateway = harness.build(SafetyConfig(monthly_token_limit=151))
    await gateway.call(harness.request())  # 150 < 151，放行
    await gateway.call(harness.request())  # 调用前不知道会用多少，最后一次可以超出
    with pytest.raises(BudgetExceededError):
        await gateway.call(harness.request())
    assert harness.ledger.tokens_used_in_month(START) == 300


async def test_one_permit_keeps_the_monthly_limit_overshoot_to_one_call(harness: Harness) -> None:
    """D117：默认改为有界并发后，只有把上限配成 1 才回到 D48 的「至多一次」语义。"""

    async def yield_to_other_tasks() -> None:
        await asyncio.sleep(0)

    organ = harness.add_organ([ok(make_attempt(50, 50))], during_call=yield_to_other_tasks)
    harness.ledger.record(ORGAN, [make_attempt(45, 45)])
    gateway = harness.build(
        SafetyConfig(monthly_token_limit=100, max_concurrent_organ_calls=1)
    )
    results = await asyncio.gather(
        gateway.call(harness.request()), gateway.call(harness.request()), return_exceptions=True
    )
    assert sum(isinstance(result, OrganResponse) for result in results) == 1
    assert sum(isinstance(result, BudgetExceededError) for result in results) == 1
    assert organ.calls == 1
    # 允许的超出只有放行那一次：90 + 100，而不是 90 + 200。
    assert harness.ledger.tokens_used_in_month(START) == 190


async def test_gateway_allows_bounded_concurrency(harness: Harness) -> None:
    """D117：最多 5 个调用同时在途，第 6 个在门外等。"""
    gate = asyncio.Event()
    inside = 0
    peak = 0

    async def hold() -> None:
        nonlocal inside, peak
        inside += 1
        peak = max(peak, inside)
        try:
            await gate.wait()
        finally:
            inside -= 1

    organ = harness.add_organ([ok()], during_call=hold)
    gateway = harness.build(SafetyConfig(max_concurrent_organ_calls=5))
    calls = [asyncio.create_task(gateway.call(harness.request())) for _ in range(6)]
    for _ in range(100):
        if organ.calls >= 5:
            break
        await asyncio.sleep(0)
    assert peak == 5
    assert organ.calls == 5  # 第 6 个还在等许可，没有进入器官

    gate.set()
    results = await asyncio.gather(*calls)
    assert all(isinstance(result, OrganResponse) for result in results)
    assert organ.calls == 6
    assert peak == 5


async def test_gateway_budget_overshoot_is_bounded_by_in_flight_calls(
    harness: Harness,
) -> None:
    """D117：检查通过时已经在途的调用都会超出，但至多 N 次。"""
    gate = asyncio.Event()

    async def hold() -> None:
        await gate.wait()

    organ = harness.add_organ(
        [ok(make_attempt(25, 25))], during_call=hold
    )
    harness.ledger.record(ORGAN, [make_attempt(45, 45)])
    gateway = harness.build(
        SafetyConfig(monthly_token_limit=100, max_concurrent_organ_calls=5)
    )
    calls = [asyncio.create_task(gateway.call(harness.request())) for _ in range(5)]
    for _ in range(100):
        if organ.calls >= 5:
            break
        await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*calls)

    assert all(isinstance(result, OrganResponse) for result in results)
    assert len(harness.ledger.entries()) == 6  # 预置的一条 + 并发放行的 5 条
    assert harness.ledger.tokens_used_in_month(START) == 90 + 5 * 50


async def test_gateway_ledger_failure_still_locks_the_gateway(harness: Harness) -> None:
    """账本写入失败后，并发进来的调用也全部被拒绝，不会再调用器官。"""

    async def break_the_ledger() -> None:
        drop_ledger_table(harness.db_path)

    organ = harness.add_organ([ok(make_attempt(10, 10))], during_call=break_the_ledger)
    gateway = harness.build(SafetyConfig(max_concurrent_organ_calls=5))
    with pytest.raises(LedgerUnavailableError, match="作废"):
        await gateway.call(harness.request())
    results = await asyncio.gather(
        *(gateway.call(harness.request()) for _ in range(4)), return_exceptions=True
    )
    assert all(isinstance(result, LedgerUnavailableError) for result in results)
    assert organ.calls == 1


async def test_runaway_guard_counts_concurrent_calls_without_losing_any(
    harness: Harness,
) -> None:
    async def yield_to_other_tasks() -> None:
        await asyncio.sleep(0)

    organ = harness.add_organ([ok()], during_call=yield_to_other_tasks)
    gateway = harness.build(
        SafetyConfig(max_calls_per_minute=3, max_concurrent_organ_calls=5)
    )
    results = await asyncio.gather(
        *(gateway.call(harness.request()) for _ in range(10)), return_exceptions=True
    )
    assert sum(isinstance(result, OrganResponse) for result in results) == 3
    assert sum(isinstance(result, RunawayBlockedError) for result in results) == 7
    assert organ.calls == 3


async def test_cancelled_call_releases_the_permit_for_the_next_one(harness: Harness) -> None:
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.sleep(3600)

    harness.add_organ([ok()], organ_id="slow", during_call=hang)
    harness.add_organ([ok()])
    # 只给一个许可，取消后如果没释放，下一次调用就会一直等在门外。
    gateway = harness.build(SafetyConfig(max_concurrent_organ_calls=1))
    slow_call = asyncio.create_task(gateway.call(harness.request("slow")))
    await started.wait()
    slow_call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow_call
    response = await asyncio.wait_for(gateway.call(harness.request()), timeout=5)
    assert response.organ_id == ORGAN


async def test_a_full_model_test_round_is_not_blocked_by_the_reply_rate_limit(
    harness: Harness,
) -> None:
    async def probe() -> OrganResponse:
        return OrganResponse("model_test", "ok", attempts=[make_attempt(1, 1)])

    gateway = harness.build(SafetyConfig(max_calls_per_minute=20))
    for _ in range(34):  # 8 个对话模型 × 4 项能力 + 2 个嵌入
        response = await gateway.test_model(probe)
        assert response.suggestion == "ok"
    entries = harness.ledger.entries()
    assert len(entries) == 34
    assert all(entry.organ_id == "model_test" for entry in entries)


async def test_model_test_is_not_rate_limited_by_default(harness: Harness) -> None:
    """D135：默认配置不限流，连续测速不会被失控拦截拒绝。"""

    async def probe() -> OrganResponse:
        return OrganResponse("model_test", "ok", attempts=[make_attempt(1, 1)])

    gateway = harness.build(SafetyConfig())
    for _ in range(100):
        response = await gateway.test_model(probe)
        assert response.suggestion == "ok"


async def test_model_test_still_respects_the_monthly_budget(harness: Harness) -> None:
    async def probe() -> OrganResponse:
        return OrganResponse("model_test", "ok", attempts=[make_attempt(100, 50)])

    gateway = harness.build(SafetyConfig(monthly_token_limit=150))
    await gateway.test_model(probe)
    with pytest.raises(BudgetExceededError):
        await gateway.test_model(probe)
    assert len(harness.ledger.entries()) == 1


async def test_model_test_does_not_take_a_reply_permit(harness: Harness) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_probe() -> OrganResponse:
        started.set()
        await release.wait()
        return OrganResponse("model_test", "ok", attempts=[make_attempt(1, 1)])

    gateway = harness.build(SafetyConfig(max_concurrent_organ_calls=1))
    probe_task = asyncio.create_task(gateway.test_model(slow_probe))
    await started.wait()

    harness.add_organ([ok(make_attempt(1, 1))])
    response = await asyncio.wait_for(gateway.call(harness.request()), timeout=5)
    assert response.suggestion == "好的"

    release.set()
    await probe_task


async def test_reply_calls_do_not_take_model_test_permits(harness: Harness) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold_reply() -> None:
        started.set()
        await release.wait()

    harness.add_organ([ok(make_attempt(1, 1))], during_call=hold_reply)
    gateway = harness.build(SafetyConfig(max_concurrent_organ_calls=1))
    reply_task = asyncio.create_task(gateway.call(harness.request()))
    await started.wait()

    async def probe() -> OrganResponse:
        return OrganResponse("model_test", "ok", attempts=[make_attempt(1, 1)])

    response = await asyncio.wait_for(gateway.test_model(probe), timeout=5)
    assert response.suggestion == "ok"

    release.set()
    await reply_task


async def test_model_test_has_its_own_concurrency_limit(harness: Harness) -> None:
    started = 0
    release = asyncio.Event()
    two_started = asyncio.Event()

    async def probe() -> OrganResponse:
        nonlocal started
        started += 1
        if started == MODEL_TEST_PERMITS:
            two_started.set()
        await release.wait()
        return OrganResponse("model_test", "ok", attempts=[make_attempt(1, 1)])

    gateway = harness.build(SafetyConfig(max_concurrent_organ_calls=5))
    tasks = [asyncio.create_task(gateway.test_model(probe)) for _ in range(3)]
    await asyncio.wait_for(two_started.wait(), timeout=5)
    for _ in range(100):
        await asyncio.sleep(0)
    assert started == MODEL_TEST_PERMITS
    assert not tasks[-1].done()

    release.set()
    await asyncio.gather(*tasks)
    assert started == 3


async def test_failed_and_estimated_usage_counts_toward_the_budget(harness: Harness) -> None:
    harness.add_organ([all_models_failed(make_attempt(50, 8192, estimated=True, succeeded=False))])
    gateway = harness.build(SafetyConfig(monthly_token_limit=8000))
    with pytest.raises(OrganCallError):
        await gateway.call(harness.request())
    with pytest.raises(BudgetExceededError):  # 一次超时就按估算记满，后面的调用被预算挡住
        await gateway.call(harness.request())


async def test_budget_resets_at_four_in_the_morning_on_the_first(harness: Harness) -> None:
    organ = harness.add_organ([ok(make_attempt(100, 50))])
    gateway = harness.build(SafetyConfig(monthly_token_limit=150))
    harness.clock.advance(datetime(2026, 12, 1, 3, 59, 59, tzinfo=UTC) - START)
    await gateway.call(harness.request())
    with pytest.raises(BudgetExceededError):
        await gateway.call(harness.request())
    harness.clock.advance(timedelta(seconds=1))  # 12 月 1 日凌晨 4 点（UTC 日历）
    await gateway.call(harness.request())
    assert organ.calls == 2


async def test_no_limit_means_no_budget_rejection(harness: Harness) -> None:
    organ = harness.add_organ([ok(make_attempt(10**6, 10**6))])
    gateway = harness.build(SafetyConfig(max_calls_per_minute=100))
    for _ in range(5):
        await gateway.call(harness.request())
    assert organ.calls == 5


# ---------- 失控拦截 ----------


async def test_rate_limit_rejects_and_recovers_after_a_minute(harness: Harness) -> None:
    organ = harness.add_organ([ok()])
    gateway = harness.build(SafetyConfig(max_calls_per_minute=2))
    await gateway.call(harness.request())
    await gateway.call(harness.request())
    with pytest.raises(RunawayBlockedError):
        await gateway.call(harness.request())
    assert organ.calls == 2
    harness.clock.advance(timedelta(minutes=1))
    await gateway.call(harness.request())
    assert organ.calls == 3


async def test_consecutive_failures_pause_the_organ(harness: Harness) -> None:
    organ = harness.add_organ([all_models_failed(make_attempt(1, 0, succeeded=False))])
    gateway = harness.build(
        SafetyConfig(max_consecutive_failures=3, failure_pause_seconds=60, max_calls_per_minute=100)
    )
    for _ in range(3):
        with pytest.raises(OrganCallError):
            await gateway.call(harness.request())
    with pytest.raises(RunawayBlockedError):
        await gateway.call(harness.request())
    assert organ.calls == 3
    harness.clock.advance(timedelta(seconds=60))
    with pytest.raises(OrganCallError):  # 暂停结束，放一次试探，仍然失败
        await gateway.call(harness.request())
    with pytest.raises(RunawayBlockedError):  # 半开：再失败一次就再暂停
        await gateway.call(harness.request())


async def test_success_resets_the_failure_count(harness: Harness) -> None:
    organ = harness.add_organ(
        [
            all_models_failed(),
            all_models_failed(),
            ok(),
            all_models_failed(),
            all_models_failed(),
            ok(),
        ]
    )
    gateway = harness.build(SafetyConfig(max_consecutive_failures=3, max_calls_per_minute=100))
    outcomes: list[str] = []
    for _ in range(6):
        try:
            await gateway.call(harness.request())
            outcomes.append("ok")
        except OrganCallError:
            outcomes.append("failed")
    assert outcomes == ["failed", "failed", "ok", "failed", "failed", "ok"]
    assert organ.calls == 6


async def test_one_organ_being_paused_does_not_stop_another(harness: Harness) -> None:
    harness.add_organ([all_models_failed()])
    other = harness.add_organ([ok(organ_id="planner")], organ_id="planner")
    gateway = harness.build(SafetyConfig(max_consecutive_failures=1))
    with pytest.raises(OrganCallError):
        await gateway.call(harness.request(ORGAN))
    with pytest.raises(RunawayBlockedError):
        await gateway.call(harness.request(ORGAN))
    await gateway.call(harness.request("planner"))
    assert other.calls == 1


async def test_organ_crash_counts_as_a_failure_and_is_reraised_unchanged(
    harness: Harness,
) -> None:
    bug = RuntimeError("器官里的 bug")
    harness.add_organ([bug])
    gateway = harness.build(SafetyConfig(max_consecutive_failures=2, max_calls_per_minute=100))
    for _ in range(2):
        with pytest.raises(RuntimeError) as info:
            await gateway.call(harness.request())
        assert info.value is bug
    with pytest.raises(RunawayBlockedError):
        await gateway.call(harness.request())
    failures = harness.events(EVENT_CALL_FAILED)
    assert len(failures) == 2
    assert "RuntimeError" in failures[0].content


async def test_disabled_guard_lets_everything_through(harness: Harness) -> None:
    organ = harness.add_organ([all_models_failed()])
    gateway = harness.build(
        SafetyConfig(
            runaway_guard_enabled=False, max_calls_per_minute=1, max_consecutive_failures=1
        )
    )
    for _ in range(5):
        with pytest.raises(OrganCallError):
            await gateway.call(harness.request())
    assert organ.calls == 5


async def test_concurrent_calls_cannot_slip_past_the_rate_limit(harness: Harness) -> None:
    async def yield_to_other_tasks() -> None:
        await asyncio.sleep(0)

    organ = harness.add_organ([ok()], during_call=yield_to_other_tasks)
    gateway = harness.build(SafetyConfig(max_calls_per_minute=1))
    results = await asyncio.gather(
        gateway.call(harness.request()), gateway.call(harness.request()), return_exceptions=True
    )
    assert sum(isinstance(result, OrganResponse) for result in results) == 1
    assert sum(isinstance(result, RunawayBlockedError) for result in results) == 1
    assert organ.calls == 1


# ---------- 账本读不了 / 写不进去 ----------


async def test_unreadable_ledger_rejects_even_without_a_limit(harness: Harness) -> None:
    organ = harness.add_organ([ok(make_attempt())])
    gateway = harness.build()  # 没设上限，也要读账本：读不了就不放行
    harness.ledger.close()
    with pytest.raises(LedgerUnavailableError):
        await gateway.call(harness.request())
    assert organ.calls == 0


async def test_broken_ledger_table_rejects_the_call(harness: Harness) -> None:
    organ = harness.add_organ([ok(make_attempt())])
    gateway = harness.build(SafetyConfig(monthly_token_limit=1000))
    drop_ledger_table(harness.db_path)
    with pytest.raises(LedgerUnavailableError):
        await gateway.call(harness.request())
    assert organ.calls == 0


async def test_failed_ledger_write_discards_the_result_and_locks_the_gateway(
    harness: Harness,
) -> None:
    async def break_the_ledger() -> None:
        drop_ledger_table(harness.db_path)

    organ = harness.add_organ([ok(make_attempt(500, 500))], during_call=break_the_ledger)
    gateway = harness.build()
    with pytest.raises(LedgerUnavailableError, match="作废"):
        await gateway.call(harness.request())  # 器官已经调用了，但账本记不上，结果不能交出去
    # 账本修好（重新打开会重新建表）之后也不放行：要重启，而不是悄悄恢复。
    harness.ledger.close()
    harness.ledger.open()
    with pytest.raises(LedgerUnavailableError, match="重启"):
        await gateway.call(harness.request())
    assert organ.calls == 1


async def test_failed_ledger_write_after_organ_failure_also_locks(harness: Harness) -> None:
    async def break_the_ledger() -> None:
        drop_ledger_table(harness.db_path)

    harness.add_organ(
        [all_models_failed(make_attempt(1, 1, succeeded=False))], during_call=break_the_ledger
    )
    gateway = harness.build()
    with pytest.raises(LedgerUnavailableError):
        await gateway.call(harness.request())
    assert len(harness.events(EVENT_CALL_FAILED)) == 1  # 器官失败这件事仍然记进了时间线


async def test_ledger_write_retries_transient_lock(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D126：短暂锁等待重试后成功，网关不锁死，下一次调用照常放行。"""
    monkeypatch.setattr("aureus.safety.ledger.LEDGER_WRITE_RETRY_DELAY_SECONDS", 0.01)
    organ = harness.add_organ([ok(make_attempt(10, 5))])
    real_write = harness.ledger._write_rows
    calls = 0

    def flaky_write(rows: Any) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise sqlite3.OperationalError("database is locked")
        real_write(rows)

    monkeypatch.setattr(harness.ledger, "_write_rows", flaky_write)
    gateway = harness.build()
    with caplog.at_level(logging.WARNING, logger="aureus.safety.ledger"):
        response = await gateway.call(harness.request())
    assert response.suggestion == "好的"
    assert calls == 3  # 第一次写入 + 2 次重试
    assert [entry.prompt_tokens for entry in harness.ledger.entries()] == [10]
    assert "重试 2 次" in caplog.text
    await gateway.call(harness.request())  # 没有置位 _ledger_write_failed
    assert organ.calls == 2


async def test_ledger_write_failure_after_retries_locks_gateway(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D126：重试后仍写不进去才锁死；不补记缺失用量，时间线保留失败。"""
    monkeypatch.setattr("aureus.safety.ledger.LEDGER_WRITE_RETRY_DELAY_SECONDS", 0.01)
    organ = harness.add_organ([ok(make_attempt(10, 5))])
    calls = 0

    def locked_write(rows: Any) -> None:
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(harness.ledger, "_write_rows", locked_write)
    gateway = harness.build()
    with pytest.raises(LedgerUnavailableError, match="作废"):
        await gateway.call(harness.request())
    assert calls == 3
    assert harness.ledger.entries() == []

    with pytest.raises(LedgerUnavailableError, match="重启"):
        await gateway.call(harness.request())
    assert organ.calls == 1  # 锁死后不再调用器官
    assert calls == 3  # 也不再尝试写账本
    assert harness.ledger.entries() == []  # 不补记缺失用量
    rejections = harness.events(EVENT_CALL_REJECTED)
    assert [record.metadata["kind"] for record in rejections] == [
        "LedgerUnavailableError",
        "LedgerUnavailableError",
    ]


async def test_ledger_write_does_not_block_event_loop(harness: Harness) -> None:
    """D126：账本在工作线程里等锁时，事件循环能推进其它协程。"""
    harness.add_organ([ok(make_attempt(10, 5))])
    gateway = harness.build()
    blocker = sqlite3.connect(str(harness.db_path), timeout=5.0)
    blocker.execute("BEGIN IMMEDIATE")  # 占住写锁，让账本写入只能等
    progressed = asyncio.Event()

    async def release_the_lock() -> None:
        await asyncio.sleep(0.05)
        progressed.set()
        blocker.commit()

    try:
        call = asyncio.create_task(gateway.call(harness.request()))
        await release_the_lock()
        assert progressed.is_set()
        # 写账本还在等锁，但事件循环已经跑了别的协程；同步写库会一直卡到锁放开之后。
        assert not call.done()
        response = await asyncio.wait_for(call, timeout=10)
    finally:
        blocker.close()
    assert response.suggestion == "好的"
    assert len(harness.ledger.entries()) == 1


# ---------- 时间线 ----------


async def test_rejections_are_recorded_as_private_events_of_mine(harness: Harness) -> None:
    harness.add_organ([ok(make_attempt(100, 50))])
    gateway = harness.build(SafetyConfig(monthly_token_limit=100))
    await gateway.call(harness.request())
    with pytest.raises(BudgetExceededError):
        await gateway.call(harness.request())
    (record,) = harness.events(EVENT_CALL_REJECTED)
    assert record.actor_type == ActorType.ME
    assert record.visibility == VisibilityScope.PRIVATE
    assert record.timestamp == START
    assert record.metadata == {"organ_id": ORGAN, "kind": "BudgetExceededError"}
    assert "150" in record.content and "100" in record.content


async def test_each_kind_of_rejection_is_recorded(harness: Harness) -> None:
    harness.add_organ([ok(make_attempt(100, 50))])
    gateway = harness.build(SafetyConfig(monthly_token_limit=100, max_calls_per_minute=1))
    await gateway.call(harness.request())
    with pytest.raises(RunawayBlockedError):
        await gateway.call(harness.request())
    harness.clock.advance(timedelta(minutes=2))
    with pytest.raises(BudgetExceededError):
        await gateway.call(harness.request())
    harness.ledger.close()
    with pytest.raises(LedgerUnavailableError):
        await gateway.call(harness.request())
    kinds = [record.metadata["kind"] for record in harness.events(EVENT_CALL_REJECTED)]
    assert kinds == ["RunawayBlockedError", "BudgetExceededError", "LedgerUnavailableError"]


async def test_rejected_calls_do_not_touch_the_ledger(harness: Harness) -> None:
    harness.add_organ([ok(make_attempt(100, 50))])
    gateway = harness.build(SafetyConfig(monthly_token_limit=100))
    await gateway.call(harness.request())
    for _ in range(3):
        with pytest.raises(BudgetExceededError):
            await gateway.call(harness.request())
    assert len(harness.ledger.entries()) == 1


async def test_halted_gateway_rejects_without_calling_or_recording_usage(
    harness: Harness,
) -> None:
    # 验收 8：急停后账本没有新增，器官根本没被调用。
    organ = harness.add_organ([ok(make_attempt(100, 50))])
    gateway = harness.build()
    harness.emergency_stop.engage("测试急停")

    with pytest.raises(PermissionDeniedError, match="急停中"):
        await gateway.call(harness.request())

    assert organ.calls == 0
    assert harness.ledger.entries() == []
    [event] = harness.events(EVENT_CALL_REJECTED)
    assert "急停中" in event.content


async def test_gateway_works_again_after_the_halt_is_lifted(harness: Harness) -> None:
    organ = harness.add_organ([ok(make_attempt(10, 5))])
    gateway = harness.build()
    harness.emergency_stop.engage()
    with pytest.raises(PermissionDeniedError):
        await gateway.call(harness.request())

    harness.emergency_stop.release()
    await gateway.call(harness.request())

    assert organ.calls == 1
    assert len(harness.ledger.entries()) == 1


async def test_halt_does_not_count_toward_the_runaway_guard(harness: Harness) -> None:
    # 急停期间被拒绝的调用不是失控，不该让失控拦截误触发。
    harness.add_organ([ok(make_attempt(1, 1))])
    gateway = harness.build(SafetyConfig(max_calls_per_minute=1))
    harness.emergency_stop.engage()
    for _ in range(3):
        with pytest.raises(PermissionDeniedError):
            await gateway.call(harness.request())
    harness.emergency_stop.release()

    await gateway.call(harness.request())


async def test_failures_are_recorded_in_ledger_and_timeline(harness: Harness) -> None:
    harness.add_organ(
        [
            all_models_failed(
                make_attempt(30, 8192, estimated=True, succeeded=False, failure_reason="超时"),
                make_attempt(0, 0, succeeded=False, failure_reason="认证失败"),
            )
        ]
    )
    gateway = harness.build()
    with pytest.raises(OrganCallError):
        await gateway.call(harness.request())
    entries = harness.ledger.entries()
    assert [(entry.estimated, entry.failure_reason) for entry in entries] == [
        (True, "超时"),
        (False, "认证失败"),
    ]
    (record,) = harness.events(EVENT_CALL_FAILED)
    assert record.visibility == VisibilityScope.PRIVATE
    assert record.metadata == {"organ_id": ORGAN}
    assert "model-a" in record.content


async def test_timeline_failure_does_not_hide_the_rejection(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.add_organ([ok(make_attempt(100, 50))])
    gateway = harness.build(SafetyConfig(monthly_token_limit=100))
    await gateway.call(harness.request())
    harness.timeline.close()
    with (
        caplog.at_level(logging.ERROR, logger="aureus.safety.gateway"),
        pytest.raises(BudgetExceededError),  # 调用方拿到的仍然是拒绝本身
    ):
        await gateway.call(harness.request())
    assert "没能记进时间线" in caplog.text


async def test_timeline_failure_does_not_hide_the_organ_failure(harness: Harness) -> None:
    harness.add_organ([all_models_failed(make_attempt(5, 5, succeeded=False))])
    gateway = harness.build()
    harness.timeline.close()
    with pytest.raises(OrganCallError):
        await gateway.call(harness.request())
    assert len(harness.ledger.entries()) == 1  # 账本照记


# ---------- 启动警告 ----------


def warnings_from(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "aureus.safety.gateway" and record.levelno == logging.WARNING
    ]


async def test_missing_limit_warns_once_at_creation(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="aureus.safety.gateway"):
        gateway = harness.build(SafetyConfig())
        harness.add_organ([ok()])
        await gateway.call(harness.request())
        await gateway.call(harness.request())
    messages = warnings_from(caplog)
    assert len(messages) == 1
    assert "monthly_token_limit" in messages[0]


async def test_disabled_guard_warns_once_at_creation(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="aureus.safety.gateway"):
        harness.build(SafetyConfig(monthly_token_limit=1000, runaway_guard_enabled=False))
    messages = warnings_from(caplog)
    assert len(messages) == 1
    assert "失控拦截" in messages[0]


async def test_both_relaxed_gives_two_warnings(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="aureus.safety.gateway"):
        harness.build(SafetyConfig(runaway_guard_enabled=False))
    assert len(warnings_from(caplog)) == 2


async def test_fully_configured_gateway_does_not_warn(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="aureus.safety.gateway"):
        harness.build(SafetyConfig(monthly_token_limit=1000))
    assert warnings_from(caplog) == []


# ---------- 异常类型 ----------


async def test_all_rejections_share_one_base_class(harness: Harness) -> None:
    for rejection in (BudgetExceededError, RunawayBlockedError, LedgerUnavailableError):
        assert issubclass(rejection, CallRejectedError)
    error = BudgetExceededError("replyer", "本月用完了")
    assert error.organ_id == "replyer"
    assert "replyer" in str(error)
