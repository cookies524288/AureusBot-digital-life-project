"""实验台 lab：只由 CLI 组装使用，运行可复现的模拟实验（D82）。"""

from aureus.lab.experiment import (
    LAB_START,
    Chart,
    Experiment,
    ExperimentResult,
    MetricValue,
    Series,
    Table,
)
from aureus.lab.registry import EXPERIMENTS
from aureus.lab.runner import RunReport, run_experiment

__all__ = [
    "EXPERIMENTS",
    "LAB_START",
    "Chart",
    "Experiment",
    "ExperimentResult",
    "MetricValue",
    "RunReport",
    "Series",
    "Table",
    "run_experiment",
]
