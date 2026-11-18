"""身体和精力闸门的测试：1.3 的验收（docs/08-roadmap.md）。

- 没设月度上限、或精力开关关着时，精力是「不限」，不拦任何调用；
- 可选的调用用到每日额度的 85% 就停，回应用户的不受影响；
- 闸门读不了账本时，可选的调用被拒绝，回应用户的照常；
- 加速时钟加假器官模拟一个月：开着精力信号不超支、不断粮；关掉后前松后紧。
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureus.body import UNLIMITED_READING, Body, EnergyTier
from aureus.common import BodyConfig, BudgetCalendar, ManualClock, SafetyConfig
from aureus.core import BiologicalState
from aureus.organs import (
    CallAttempt,
    CallPurpose,
    OrganBus,
    OrganRequest,
    OrganResponse,
)
from aureus.safety import (
    BudgetExceededError,
    EmergencyStop,
    EnergyDepletedError,
    Ledger,
    PermissionChecker,
    SafetyGateway,
)
from aureus.timeline import TimelineDatabase

START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)  # 当地（UTC）11 月 1 日中午
CALENDAR = BudgetCalendar(UTC)
ORGAN = "replyer"
LIMIT = 3_000_000  # 30 天，每天 100,000
CALL_TOKENS = 1_000  # 一次调用用的 token（输入 800 + 输出 200）


class TokenOrgan:
    """每次调用花固定 token 的假器官。"""

    organ_id = ORGAN

    def __init__(self) -> None:
        self.calls = 0

    async def process(self, request: OrganRequest) -> OrganResponse:
        self.calls += 1
        attempt = CallAttempt(
            model="m", provider="p", prompt_tokens=CALL_TOKENS * 4 // 5,
            completion_tokens=CALL_TOKENS // 5,
        )  # fmt: skip
        return OrganResponse(organ_id=ORGAN, suggestion="好", attempts=[attempt])


@dataclass
class Rig:
    clock: ManualClock
    ledger: Ledger
    timeline: TimelineDatabase
    db_path: Path
    bio_state: BiologicalState
    organ: TokenOrgan
    stop: EmergencyStop
    bus: OrganBus

    def body(self, limit: int | None = LIMIT, **config: Any) -> Body:
        return Body(BodyConfig(**config), limit, self.ledger, self.clock, CALENDAR)

    def gateway(self, body: Body | None, limit: int | None = LIMIT) -> SafetyGateway:
        return SafetyGateway(
            self.bus,
            self.ledger,
            self.timeline,
            self.clock,
            SafetyConfig(
                monthly_token_limit=limit,
                runaway_guard_enabled=False,  # 模拟一个月要调用上千次，失控拦截另有测试
            ),
            PermissionChecker(self.stop),
            energy_gate=body,
        )

    def request(self, purpose: CallPurpose = CallPurpose.OPTIONAL) -> OrganRequest:
        return OrganRequest(
            organ_id=ORGAN, prompt="你好", bio_state=self.bio_state, purpose=purpose
        )

    def go_to(self, moment: datetime) -> None:
        self.clock.advance(moment - self.clock.now())

    def spend(self, tokens: int) -> None:
        """直接往账本记一笔用量（不经过网关）。"""
        attempt = CallAttempt(model="m", provider="p", prompt_tokens=tokens, completion_tokens=0)
        self.ledger.record(ORGAN, [attempt])


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    clock = ManualClock(START)
    db_path = tmp_path / "aureus.db"
    ledger = Ledger(db_path, clock, calendar=CALENDAR)
    timeline = TimelineDatabase(db_path, clock)
    ledger.open()
    timeline.open()
    organ = TokenOrgan()
    bus = OrganBus()
    bus.register(organ)
    yield Rig(
        clock, ledger, timeline, db_path, BiologicalState(clock=clock), organ,
        EmergencyStop(tmp_path / "halt.json", clock), bus,
    )  # fmt: skip
    ledger.close()
    timeline.close()


# ---------- 读数 ----------


def test_no_monthly_limit_means_unlimited(rig: Rig) -> None:
    rig.spend(10_000_000)
    reading = rig.body(limit=None).read_energy()
    assert reading is UNLIMITED_READING
    assert reading.tier is EnergyTier.UNLIMITED
    assert not reading.limited
    assert reading.fraction is None


def test_energy_switch_off_means_unlimited(rig: Rig) -> None:
    rig.spend(500_000)
    assert rig.body(energy_enabled=False).read_energy() is UNLIMITED_READING


def test_reading_follows_the_ledger(rig: Rig) -> None:
    body = rig.body()
    assert body.read_energy().tier is EnergyTier.NORMAL
    rig.spend(60_000)
    reading = body.read_energy()
    assert reading.used_today == 60_000
    assert reading.remaining_today == 40_000
    assert reading.tier is EnergyTier.SAVING
    assert reading.days_left_in_month == 30


def test_next_day_refills_and_carries_the_leftover(rig: Rig) -> None:
    rig.spend(60_000)
    rig.clock.advance(timedelta(days=1))  # 11 月 2 日中午
    reading = rig.body().read_energy()
    assert reading.used_today == 0
    assert reading.daily_quota == (LIMIT - 60_000) // 29  # 省下的摊给剩下的 29 天
    assert reading.tier is EnergyTier.NORMAL


def test_day_rolls_over_at_4am_not_midnight(rig: Rig) -> None:
    rig.go_to(datetime(2026, 11, 1, 23, 0, tzinfo=UTC))
    rig.spend(90_000)
    rig.go_to(datetime(2026, 11, 2, 3, 59, tzinfo=UTC))  # 过了零点，还是 11 月 1 日
    assert rig.body().read_energy().used_today == 90_000
    rig.go_to(datetime(2026, 11, 2, 4, 0, tzinfo=UTC))
    assert rig.body().read_energy().used_today == 0


def test_ledger_failure_propagates_from_read_energy(rig: Rig) -> None:
    drop_ledger_table(rig.db_path)
    with pytest.raises(sqlite3.Error):
        rig.body().read_energy()


# ---------- 闸门 ----------


def test_optional_calls_stop_at_85_percent_of_the_daily_quota(rig: Rig) -> None:
    body = rig.body()
    rig.spend(84_999)
    assert body.refusal_reason(CallPurpose.OPTIONAL) is None
    rig.spend(1)  # 刚好 85,000
    reason = body.refusal_reason(CallPurpose.OPTIONAL)
    assert reason is not None
    assert "85000" in reason
    assert "明天" in reason


def test_user_replies_ignore_the_daily_quota(rig: Rig) -> None:
    rig.spend(500_000)  # 比今天的额度多得多
    assert rig.body().refusal_reason(CallPurpose.USER_REPLY) is None


def test_the_gate_lets_everything_through_when_unlimited(rig: Rig) -> None:
    rig.spend(500_000)
    assert rig.body(limit=None).refusal_reason(CallPurpose.OPTIONAL) is None
    assert rig.body(energy_enabled=False).refusal_reason(CallPurpose.OPTIONAL) is None


def test_the_default_purpose_is_optional() -> None:
    # 忘了标用途时按可选处理，不确定就朝省的方向倒（04 号文档第 4 节）。
    request = OrganRequest(organ_id=ORGAN, prompt="x", bio_state=BiologicalState())
    assert request.purpose is CallPurpose.OPTIONAL


@pytest.mark.asyncio
async def test_gateway_rejects_optional_calls_after_the_quota_but_not_replies(rig: Rig) -> None:
    gateway = rig.gateway(rig.body())
    rig.spend(90_000)

    with pytest.raises(EnergyDepletedError):
        await gateway.call(rig.request(CallPurpose.OPTIONAL))
    assert rig.organ.calls == 0

    response = await gateway.call(rig.request(CallPurpose.USER_REPLY))
    assert response.suggestion == "好"
    assert rig.organ.calls == 1


@pytest.mark.asyncio
async def test_a_rejected_optional_call_is_noted_in_the_timeline(rig: Rig) -> None:
    gateway = rig.gateway(rig.body())
    rig.spend(90_000)
    with pytest.raises(EnergyDepletedError):
        await gateway.call(rig.request())
    from aureus.safety import EVENT_CALL_REJECTED
    from aureus.timeline import TimelineQuery

    records = list(rig.timeline.query(TimelineQuery(event_type=EVENT_CALL_REJECTED)))
    assert len(records) == 1
    assert "精力" in records[0].content


@pytest.mark.asyncio
async def test_gateway_without_a_body_does_not_limit_by_energy(rig: Rig) -> None:
    gateway = rig.gateway(None)
    rig.spend(90_000)
    await gateway.call(rig.request())
    assert rig.organ.calls == 1


@pytest.mark.asyncio
async def test_unreadable_ledger_rejects_optional_but_not_user_replies(rig: Rig) -> None:
    # 精力闸门读不了账本时朝安全方向倒；回应用户的调用不会问闸门，
    # 但网关自己读月度用量也要用账本，所以这里用一个只有闸门坏掉的替身。
    class BrokenGate:
        def refusal_reason(self, purpose: CallPurpose) -> str | None:
            raise sqlite3.OperationalError("disk I/O error")

    gateway = SafetyGateway(
        rig.bus, rig.ledger, rig.timeline, rig.clock, SafetyConfig(),
        PermissionChecker(rig.stop), energy_gate=BrokenGate(),
    )  # fmt: skip
    with pytest.raises(EnergyDepletedError):
        await gateway.call(rig.request(CallPurpose.OPTIONAL))
    assert rig.organ.calls == 0
    await gateway.call(rig.request(CallPurpose.USER_REPLY))
    assert rig.organ.calls == 1


@pytest.mark.asyncio
async def test_the_monthly_limit_still_applies_to_user_replies(rig: Rig) -> None:
    gateway = rig.gateway(rig.body(), limit=LIMIT)
    rig.spend(LIMIT)
    with pytest.raises(BudgetExceededError):
        await gateway.call(rig.request(CallPurpose.USER_REPLY))


# ---------- 模拟一个月 ----------

# 月上限取小一点，模拟一个月才不用调用上千次：30 天，每天 10,000，每次 1,000。
SIM_LIMIT = 300_000
SIM_DAILY_QUOTA = 10_000
CALLS_PER_DAY_TRIED = 20  # 每天想打 20 次，是每日额度的两倍


async def simulate_month(rig: Rig, gateway: SafetyGateway) -> list[int]:
    """每天想用两倍额度，一直试到被拒绝或当天试完，返回每天实际花的 token。"""
    daily_spend: list[int] = []
    for _ in range(30):
        before = rig.ledger.tokens_used_in_day(rig.clock.now())
        for _ in range(CALLS_PER_DAY_TRIED):
            try:
                await gateway.call(rig.request())
            except (EnergyDepletedError, BudgetExceededError):
                break
        daily_spend.append(rig.ledger.tokens_used_in_day(rig.clock.now()) - before)
        rig.clock.advance(timedelta(days=1))
    return daily_spend


@pytest.mark.asyncio
async def test_with_energy_on_the_month_never_overspends_and_never_runs_dry(rig: Rig) -> None:
    gateway = rig.gateway(rig.body(limit=SIM_LIMIT), limit=SIM_LIMIT)
    daily_spend = await simulate_month(rig, gateway)

    assert sum(daily_spend) <= SIM_LIMIT
    assert all(spend > 0 for spend in daily_spend)  # 每天都还有精力，没有断粮
    # 第一天最多用到额度的 85%（最后一次调用可能略超一次的量）。
    assert daily_spend[0] <= SIM_DAILY_QUOTA * 0.85 + CALL_TOKENS
    # 每天剩下的 15% 摊给后面，所以越往后每天能用的越多，最后一天最宽裕。
    assert daily_spend[-1] > daily_spend[0]


@pytest.mark.asyncio
async def test_with_energy_off_the_month_is_front_loaded(rig: Rig) -> None:
    gateway = rig.gateway(rig.body(limit=SIM_LIMIT, energy_enabled=False), limit=SIM_LIMIT)
    daily_spend = await simulate_month(rig, gateway)

    assert sum(daily_spend) <= SIM_LIMIT + CALL_TOKENS  # 月度上限还是兜住的
    assert daily_spend[0] == CALLS_PER_DAY_TRIED * CALL_TOKENS  # 第一天放开花
    assert daily_spend[-1] == 0  # 月底断粮：前松后紧
    assert daily_spend.count(0) >= 10


def drop_ledger_table(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE ledger")
        conn.commit()
    finally:
        conn.close()
