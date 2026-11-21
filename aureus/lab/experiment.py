"""实验台 lab：实验接口和结果数据结构（D82）。"""

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LAB_START = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)

MetricValue = int | float | bool | str


def _validate_artifact_name(name: str) -> None:
    if re.fullmatch(r"[a-z0-9_]+", name) is None:
        raise ValueError("表格和图的名字只能包含小写英文字母、数字和下划线")


@dataclass(frozen=True)
class Table:
    """一张表，写成 CSV。"""

    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[MetricValue, ...], ...]

    def __post_init__(self) -> None:
        _validate_artifact_name(self.name)


@dataclass(frozen=True)
class Series:
    """折线图中的一条数据线。"""

    label: str
    x: tuple[float, ...]
    y: tuple[float, ...]


@dataclass(frozen=True)
class Chart:
    """一张折线图，画成 PNG。"""

    name: str
    title: str
    x_label: str
    y_label: str
    series: tuple[Series, ...]

    def __post_init__(self) -> None:
        _validate_artifact_name(self.name)


@dataclass(frozen=True)
class ExperimentResult:
    """一次实验产生的指标、表和图数据。"""

    metrics: Mapping[str, MetricValue]
    tables: tuple[Table, ...]
    charts: tuple[Chart, ...]


@dataclass(frozen=True)
class Experiment:
    """实验登记信息，以及执行实验的异步函数。"""

    name: str
    code: str
    title: str
    parameters: Mapping[str, MetricValue]
    run: Callable[[Mapping[str, MetricValue], int, Path], Awaitable[ExperimentResult]]
