"""激素子系统：怒气、愉悦、低落、惊讶四种短期情绪（D69，路线图 2.3）。

由事件触发，按半衰期消退，浓度由代码算，不由大模型决定。
"""

from .effects import Conflict, HormoneEffects, RetaliationMode
from .hormones import (
    HormoneKind,
    HormoneTrigger,
    kind_enabled,
    level_at,
    make_triggers_from_empathy,
    make_triggers_from_feedback,
    make_triggers_from_judgment,
)
from .state import Hormones

__all__ = [
    "Conflict",
    "HormoneEffects",
    "HormoneKind",
    "HormoneTrigger",
    "Hormones",
    "RetaliationMode",
    "kind_enabled",
    "level_at",
    "make_triggers_from_empathy",
    "make_triggers_from_feedback",
    "make_triggers_from_judgment",
]
