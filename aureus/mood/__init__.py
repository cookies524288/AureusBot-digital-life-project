"""心情子系统：几小时到几天的基调（D73，路线图 2.6）。

由激素触发和反馈结果累积、按半衰期回落，再减去此刻的压力；不由大模型决定。
"""

from .mood import (
    FeedbackOutcome,
    MoodContribution,
    MoodReading,
    describe_mood,
    from_outcomes,
    from_triggers,
    valence_at,
)
from .state import Mood

__all__ = [
    "FeedbackOutcome",
    "Mood",
    "MoodContribution",
    "MoodReading",
    "describe_mood",
    "from_outcomes",
    "from_triggers",
    "valence_at",
]
