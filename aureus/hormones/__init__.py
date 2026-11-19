"""激素子系统：怒气、愉悦、低落、惊讶四种短期情绪（D69，路线图 2.3）。

由事件触发，按半衰期消退，浓度由代码算，不由大模型决定。
"""

from .hormones import (
    HormoneKind,
    HormoneTrigger,
    level_at,
    make_triggers_from_feedback,
    make_triggers_from_judgment,
)
from .state import Hormones

__all__ = [
    "HormoneKind",
    "HormoneTrigger",
    "Hormones",
    "level_at",
    "make_triggers_from_feedback",
    "make_triggers_from_judgment",
]
