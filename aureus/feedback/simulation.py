"""模拟任务和校准误差：验证自信会逐渐接近真实成功率（路线图 2.1，D67 第 4 条）。

模拟任务是固定随机种子、成功率事先已知的假任务。每一轮每个任务：先按当前自信写下预期，
再抽签决定成败，最后补写结果。不调用任何模型，不花钱，同一个种子结果完全一样。
完整的实验台和脚本留给 2.11（D66），那时复用这里的东西。
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass

from .store import PRIOR_CONFIDENCE, SIM_TASK_PREFIX, FeedbackStore, Outcome

# 算校准误差时把 [0, 1] 的预期分成几格；10 格是校准误差最常用的分法。
CALIBRATION_BINS = 10


@dataclass(frozen=True)
class SimulatedTask:
    """一种模拟任务：名字和事先已知的真实成功率。"""

    name: str
    success_rate: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.success_rate <= 1.0:
            raise ValueError(f"成功率要在 0 到 1 之间，收到 {self.success_rate}")

    @property
    def category(self) -> str:
        """反馈里的类别名：`sim_task:<名字>`。"""
        return SIM_TASK_PREFIX + self.name


@dataclass(frozen=True)
class SimulatedAttempt:
    """一次模拟行动：行动前说的把握，和实际成败。"""

    category: str
    expected: float
    succeeded: bool


def run_simulation(
    store: FeedbackStore, tasks: Sequence[SimulatedTask], rounds: int, seed: int
) -> list[SimulatedAttempt]:
    """跑若干轮模拟任务，把预期和结果都记进反馈，返回每次行动的预期和成败。

    预期取当时反馈给出的自信；这一类还没有历史就用先验，和没有数据时的保守说法一致。

    Args:
        store: 已打开的反馈存储。
        tasks: 要模拟的任务。
        rounds: 轮数，每轮每个任务各做一次。
        seed: 随机种子；同样的种子得到同样的结果。
    """
    rng = random.Random(seed)
    attempts: list[SimulatedAttempt] = []
    for _ in range(rounds):
        for task in tasks:
            confidence = store.confidence(task.category).value
            expected = PRIOR_CONFIDENCE if confidence is None else confidence
            record_id = store.record_expectation(task.category, expected)
            succeeded = rng.random() < task.success_rate
            store.record_outcome(record_id, Outcome.SUCCESS if succeeded else Outcome.FAILURE)
            attempts.append(SimulatedAttempt(task.category, expected, succeeded))
    return attempts


def expected_calibration_error(attempts: Sequence[SimulatedAttempt]) -> float:
    """期望校准误差（ECE）：说的把握和实际成功率差多少，越小越准。

    按预期把握分格；每格算「平均预期」和「实际成功率」的差，再按格里的次数加权平均。
    """
    if not attempts:
        raise ValueError("没有行动，算不出校准误差")
    bins: list[list[SimulatedAttempt]] = [[] for _ in range(CALIBRATION_BINS)]
    for attempt in attempts:
        # 预期恰好是 1.0 时放进最后一格。
        index = min(int(attempt.expected * CALIBRATION_BINS), CALIBRATION_BINS - 1)
        bins[index].append(attempt)
    error = 0.0
    for members in bins:
        if not members:
            continue
        mean_expected = sum(member.expected for member in members) / len(members)
        success_rate = sum(member.succeeded for member in members) / len(members)
        error += abs(mean_expected - success_rate) * len(members) / len(attempts)
    return error
