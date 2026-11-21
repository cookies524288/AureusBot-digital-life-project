"""E2 精力调节：以假器官和同一份模拟日程比较精力开关（D82）。"""

import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, timedelta
from enum import StrEnum
from pathlib import Path

from aureus.body import Body
from aureus.common import BodyConfig, BudgetCalendar, ManualClock, SafetyConfig
from aureus.lab.experiment import (
    LAB_START,
    Chart,
    Experiment,
    ExperimentResult,
    MetricValue,
    Series,
    Table,
)
from aureus.organs import CallAttempt, CallPurpose, OrganBus, OrganRequest, OrganResponse
from aureus.safety import (
    BudgetExceededError,
    EmergencyStop,
    EnergyDepletedError,
    Ledger,
    PermissionChecker,
    SafetyGateway,
)
from aureus.timeline import TimelineDatabase

ENERGY_PARAMETERS: Mapping[str, MetricValue] = {
    "monthly_token_limit": 300_000,
    "days": 30,
    "tokens_per_call": 500,
    "user_messages_min": 1,
    "user_messages_max": 3,
    "optional_wishes_per_day": 40,
}
ORGAN_ID = "replyer"


class EventKind(StrEnum):
    """模拟日程中的用户消息和可选愿望。"""

    USER_MESSAGE = "user_message"
    OPTIONAL_WISH = "optional_wish"


class TokenOrgan:
    """每次调用报告固定虚拟用量的假器官（D15、D82）。"""

    organ_id = ORGAN_ID

    def __init__(self, tokens_per_call: int) -> None:
        self._tokens_per_call = tokens_per_call

    async def process(self, request: OrganRequest) -> OrganResponse:
        """返回固定建议和固定输入、输出 token 数。"""
        prompt_tokens = self._tokens_per_call * 4 // 5
        completion_tokens = self._tokens_per_call - prompt_tokens
        attempt = CallAttempt(
            model="m",
            provider="p",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return OrganResponse(organ_id=self.organ_id, suggestion="好", attempts=[attempt])


@dataclass(frozen=True)
class ScenarioResult:
    """精力开关一侧的逐日花费和月度指标来源。"""

    daily_spend: tuple[int, ...]
    month_spend: int
    overspent_days: int
    starved_days: int
    user_messages: int
    user_responses: int
    optional_done: int

    @property
    def user_response_rate(self) -> float:
        """用户消息的成功回应比例；没有用户消息时按 100% 处理。"""
        if self.user_messages == 0:
            return 1.0
        return self.user_responses / self.user_messages


def build_schedule(parameters: Mapping[str, MetricValue], seed: int) -> list[list[EventKind]]:
    """按种子生成整月规则日程，每天打乱用户消息和可选愿望。"""
    rng = random.Random(seed)
    schedule: list[list[EventKind]] = []
    for _ in range(int(parameters["days"])):
        user_messages = rng.randint(
            int(parameters["user_messages_min"]), int(parameters["user_messages_max"])
        )
        events = [EventKind.USER_MESSAGE] * user_messages
        events.extend([EventKind.OPTIONAL_WISH] * int(parameters["optional_wishes_per_day"]))
        rng.shuffle(events)
        schedule.append(events)
    return schedule


async def run_energy(
    parameters: Mapping[str, MetricValue], seed: int, run_dir: Path
) -> ExperimentResult:
    """用同一份 30 日日程分别运行精力开启和关闭的安全网关。"""
    schedule = build_schedule(parameters, seed)
    monthly_limit = int(parameters["monthly_token_limit"])
    days = int(parameters["days"])
    tokens_per_call = int(parameters["tokens_per_call"])
    on = await _run_scenario(schedule, monthly_limit, tokens_per_call, run_dir, energy_enabled=True)
    off = await _run_scenario(
        schedule, monthly_limit, tokens_per_call, run_dir, energy_enabled=False
    )
    metrics: dict[str, MetricValue] = {}
    for prefix, result in (("on", on), ("off", off)):
        metrics.update(
            {
                f"{prefix}.month_spend": result.month_spend,
                f"{prefix}.overspent_days": result.overspent_days,
                f"{prefix}.starved_days": result.starved_days,
                f"{prefix}.user_messages": result.user_messages,
                f"{prefix}.user_response_rate": result.user_response_rate,
                f"{prefix}.optional_done": result.optional_done,
            }
        )

    on_cumulative = _cumulative_spend(on.daily_spend)
    off_cumulative = _cumulative_spend(off.daily_spend)
    even_pace = tuple(monthly_limit * day / days for day in range(1, days + 1))
    daily = Table(
        name="daily",
        columns=("day", "on_spend", "off_spend", "on_cumulative", "off_cumulative", "even_pace"),
        rows=tuple(
            (
                day,
                on.daily_spend[day - 1],
                off.daily_spend[day - 1],
                on_cumulative[day - 1],
                off_cumulative[day - 1],
                even_pace[day - 1],
            )
            for day in range(1, days + 1)
        ),
    )
    day_numbers = tuple(float(day) for day in range(1, days + 1))
    charts = (
        Chart(
            name="daily_spend",
            title="每天的 token 花费",
            x_label="第几天",
            y_label="当天花费（token）",
            series=(
                Series(label="开着精力", x=day_numbers, y=tuple(map(float, on.daily_spend))),
                Series(label="关掉精力", x=day_numbers, y=tuple(map(float, off.daily_spend))),
            ),
        ),
        Chart(
            name="cumulative_spend",
            title="累计 token 花费",
            x_label="第几天",
            y_label="累计花费（token）",
            series=(
                Series(label="开着精力", x=day_numbers, y=tuple(map(float, on_cumulative))),
                Series(label="关掉精力", x=day_numbers, y=tuple(map(float, off_cumulative))),
                Series(label="均匀进度", x=day_numbers, y=even_pace),
            ),
        ),
    )
    return ExperimentResult(metrics=metrics, tables=(daily,), charts=charts)


async def _run_scenario(
    schedule: list[list[EventKind]],
    monthly_limit: int,
    tokens_per_call: int,
    run_dir: Path,
    *,
    energy_enabled: bool,
) -> ScenarioResult:
    """将一个精力开关条件接入真实账本、身体闸门和安全网关。"""
    clock = ManualClock(LAB_START)
    calendar = BudgetCalendar(UTC)
    database_name = "energy_on.db" if energy_enabled else "energy_off.db"
    halt_name = "halt_on.json" if energy_enabled else "halt_off.json"
    database_path = run_dir / database_name
    ledger = Ledger(database_path, clock, calendar=calendar)
    timeline = TimelineDatabase(database_path, clock)
    organ_bus = OrganBus()
    organ_bus.register(TokenOrgan(tokens_per_call))
    body = Body(BodyConfig(energy_enabled=energy_enabled), monthly_limit, ledger, clock, calendar)
    gateway = SafetyGateway(
        organ_bus,
        ledger,
        timeline,
        clock,
        SafetyConfig(monthly_token_limit=monthly_limit, runaway_guard_enabled=False),
        PermissionChecker(EmergencyStop(run_dir / halt_name, clock)),
        energy_gate=body,
    )
    ledger.open()
    daily_spend: list[int] = []
    cumulative_spend = 0
    overspent_days = 0
    starved_days = 0
    user_messages = 0
    user_responses = 0
    optional_done = 0
    try:
        timeline.open()
        for day, events in enumerate(schedule):
            day_start = LAB_START + timedelta(days=day, hours=8)
            clock.advance(day_start - clock.now())
            user_budget_rejected = False
            for event in events:
                purpose = (
                    CallPurpose.USER_REPLY
                    if event is EventKind.USER_MESSAGE
                    else CallPurpose.OPTIONAL
                )
                if event is EventKind.USER_MESSAGE:
                    user_messages += 1
                try:
                    await gateway.call(
                        OrganRequest(organ_id=ORGAN_ID, prompt="你好", purpose=purpose)
                    )
                    if event is EventKind.USER_MESSAGE:
                        user_responses += 1
                    else:
                        optional_done += 1
                except (EnergyDepletedError, BudgetExceededError) as error:
                    if event is EventKind.USER_MESSAGE and isinstance(error, BudgetExceededError):
                        user_budget_rejected = True
                clock.advance(timedelta(minutes=1))
            day_spend = ledger.tokens_used_in_day(clock.now())
            daily_spend.append(day_spend)
            cumulative_spend += day_spend
            if cumulative_spend > monthly_limit * (day + 1) / len(schedule):
                overspent_days += 1
            if user_budget_rejected:
                starved_days += 1
        month_spend = ledger.tokens_used_in_month(clock.now())
    finally:
        timeline.close()
        ledger.close()
    return ScenarioResult(
        daily_spend=tuple(daily_spend),
        month_spend=month_spend,
        overspent_days=overspent_days,
        starved_days=starved_days,
        user_messages=user_messages,
        user_responses=user_responses,
        optional_done=optional_done,
    )


def _cumulative_spend(daily_spend: tuple[int, ...]) -> tuple[int, ...]:
    total = 0
    result: list[int] = []
    for spend in daily_spend:
        total += spend
        result.append(total)
    return tuple(result)


ENERGY_EXPERIMENT = Experiment(
    name="energy",
    code="E2",
    title="精力调节：开、关精力信号各跑一个月",
    parameters=ENERGY_PARAMETERS,
    run=run_energy,
)
