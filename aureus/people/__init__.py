"""人际 people：人物档案、账号和好感（路线图 1.8、2.4，05 号文档，D71）。"""

from aureus.people.affinity import (
    AffinityBook,
    AffinityChange,
    AffinityEvent,
    AffinityReason,
    describe_affinity,
)
from aureus.people.directory import PeopleDirectory, Person, PersonKind
from aureus.people.history import (
    HistoricalEntry,
    HistoricalKind,
    HistoricalPublication,
    HistoricalRecord,
    PeopleError,
)

__all__ = [
    "AffinityBook",
    "AffinityChange",
    "AffinityEvent",
    "AffinityReason",
    "HistoricalEntry",
    "HistoricalKind",
    "HistoricalPublication",
    "HistoricalRecord",
    "PeopleDirectory",
    "PeopleError",
    "Person",
    "PersonKind",
    "describe_affinity",
]
