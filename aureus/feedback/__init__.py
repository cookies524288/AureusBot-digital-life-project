"""反馈 feedback：做事前记预期，做完记结果，用真实成功率算自信（D67）。"""

from .simulation import (
    SimulatedAttempt,
    SimulatedTask,
    expected_calibration_error,
    run_simulation,
)
from .store import (
    COMMITMENT_KEPT,
    PRIOR_CONFIDENCE,
    PROACTIVE_CONTACT,
    SIM_TASK_PREFIX,
    Confidence,
    FeedbackError,
    FeedbackRecord,
    FeedbackStore,
    Outcome,
    check_category,
)

__all__ = [
    "COMMITMENT_KEPT",
    "PRIOR_CONFIDENCE",
    "PROACTIVE_CONTACT",
    "SIM_TASK_PREFIX",
    "Confidence",
    "FeedbackError",
    "FeedbackRecord",
    "FeedbackStore",
    "Outcome",
    "SimulatedAttempt",
    "SimulatedTask",
    "check_category",
    "expected_calibration_error",
    "run_simulation",
]
