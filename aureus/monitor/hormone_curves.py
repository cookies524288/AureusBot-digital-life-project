"""观察台的激素曲线：从时间线里的触发记录用 level_at 算出来，只读，不加表（D69 第 8 条）。

这里不 import streamlit，测试不用开浏览器。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from aureus.common import HormonesConfig
from aureus.hormones import HormoneKind, HormoneTrigger, level_at
from aureus.hormones.hormones import half_life_seconds_of

logger = logging.getLogger(__name__)

# 曲线上画多少个点：24 小时、每 10 分钟一个点，够看出怒气几分钟尺度的起落。
CURVE_POINTS = 145
# 触发过了这么多个半衰期就衰减到不足 0.4%，不用再读（和心跳重启还原的窗口一致）。
_AFTERGLOW_HALF_LIVES = 8


def hormone_curves(
    records: Iterable[Mapping[str, object]],
    start: datetime,
    end: datetime,
    config: HormonesConfig,
    points: int = CURVE_POINTS,
) -> tuple[list[datetime], dict[HormoneKind, list[float]]]:
    """算 [start, end] 之间等间隔的每种激素浓度。

    Args:
        records: hormone_triggered 记录的 metadata；看不懂的跳过并记警告。
        start: 曲线起点（UTC）。
        end: 曲线终点（UTC），必须晚于起点。
        config: 激素配置（半衰期、哪种关着）。
        points: 点数，至少 2。

    Returns:
        （各点的时刻，每种激素在这些时刻的浓度）。关着的激素浓度全是 0。

    Raises:
        ValueError: end 不晚于 start，或点数不足 2。
    """
    if end <= start:
        raise ValueError("曲线终点必须晚于起点")
    if points < 2:
        raise ValueError("曲线至少要 2 个点")
    triggers: list[HormoneTrigger] = []
    for metadata in records:
        trigger = HormoneTrigger.from_metadata(metadata)
        if trigger is None:
            logger.warning("观察台：看不懂的 hormone_triggered 记录，已跳过：%r", metadata)
            continue
        triggers.append(trigger)
    step = (end - start) / (points - 1)
    moments = [start + step * index for index in range(points)]
    curves = {
        kind: [level_at(triggers, kind, moment, config) for moment in moments]
        for kind in HormoneKind
    }
    return moments, curves


def lookback(hours: int, config: HormonesConfig) -> timedelta:
    """读触发记录要往前多看多久：曲线窗口加最长半衰期的 8 倍，让窗口开头也带着余波。"""
    longest = max(half_life_seconds_of(kind, config) for kind in HormoneKind)
    return timedelta(hours=hours, seconds=longest * _AFTERGLOW_HALF_LIVES)
