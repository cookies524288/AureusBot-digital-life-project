"""小探索的把关、兴趣选择和严格总结解析；不读写状态（D79）。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from aureus.body import EnergyTier
from aureus.common import ExplorationConfig
from aureus.heartbeat.planning import first_json_object

_ALLOWED_TIERS = (EnergyTier.UNLIMITED, EnergyTier.NORMAL)
_MAX_SUMMARY_CHARS = 4000
MAX_FINDING_CHARS = 200


@dataclass(frozen=True)
class InterestStatus:
    """一个在用兴趣及其上次开始探索的时刻。"""

    interest_id: int
    last_explored_at: datetime | None = None


@dataclass(frozen=True)
class ExplorationState:
    """从探索开始记录恢复的次数和间隔依据。"""

    explorations_today: int = 0
    last_started_at: datetime | None = None


@dataclass(frozen=True)
class ExplorationDecision:
    """第一道未满足的条件，或选定的兴趣。"""

    allowed: bool
    blocked_by: str = ""
    interest_id: int | None = None


class ExplorationResult(StrEnum):
    """探索结果的固定取值。"""

    FOUND = "found"
    BORING = "boring"
    NO_RESULTS = "no_results"
    SEARCH_FAILED = "search_failed"
    FETCH_FAILED = "fetch_failed"
    SUMMARY_FAILED = "summary_failed"
    BLOCKED = "blocked"
    RECORD_FAILED = "record_failed"


def daily_limit(config: ExplorationConfig, *, cautious: bool) -> int:
    """谨慎时每日次数减半，至少一次。"""
    return max(1, config.max_per_day // 2) if cautious else config.max_per_day


def decide(
    *,
    now: datetime,
    config: ExplorationConfig,
    state: ExplorationState,
    interests: Sequence[InterestStatus],
    effective_tier: EnergyTier | None,
    night: bool,
    halted: bool,
    busy: bool,
    cautious: bool,
    has_search: bool,
    has_organ: bool,
) -> ExplorationDecision:
    """按规定顺序把关，先选从未探索的，再选最久没探索的（同刻编号小的优先）。"""
    checks = (
        (config.enabled, "disabled"),
        (has_search, "no_search"),
        (has_organ, "no_organ"),
        (not halted, "halted"),
        (not busy, "busy"),
        (effective_tier in _ALLOWED_TIERS, "tier"),
        (not night, "night"),
        (state.explorations_today < daily_limit(config, cautious=cautious), "daily_limit"),
        (
            state.last_started_at is None
            or now - state.last_started_at >= timedelta(hours=config.min_interval_hours),
            "interval",
        ),
        (bool(interests), "no_interest"),
    )
    for allowed, reason in checks:
        if not allowed:
            return ExplorationDecision(False, reason)
    selected = min(
        interests,
        key=lambda item: (
            item.last_explored_at is not None,
            item.last_explored_at or now,
            item.interest_id,
        ),
    )
    return ExplorationDecision(True, interest_id=selected.interest_id)


@dataclass(frozen=True)
class Summary:
    """总结器官只可提出是否有意思和一句资料。"""

    interesting: bool
    finding: str


def parse_summary(text: str) -> Summary | None:
    """字段、类型或长度有一处不合格，整份总结作废。"""
    if len(text) > _MAX_SUMMARY_CHARS:
        return None
    data = first_json_object(text)
    if data is None or set(data) != {"interesting", "finding"}:
        return None
    interesting, finding = data["interesting"], data["finding"]
    if not isinstance(interesting, bool) or not isinstance(finding, str):
        return None
    finding = finding.strip()
    if len(finding) > MAX_FINDING_CHARS or (interesting and not finding):
        return None
    return Summary(interesting, finding)
