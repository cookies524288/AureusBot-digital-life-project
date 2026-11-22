"""人际 people：人物档案、账号和好感（路线图 1.8、2.4，05 号文档，D71）。"""

from aureus.people.account_link import LINK_COMMAND, LINK_LIFETIME, AccountLink
from aureus.people.affinity import (
    AffinityBook,
    AffinityChange,
    AffinityEvent,
    AffinityReason,
    describe_affinity,
)
from aureus.people.directory import PeopleDirectory, PeopleError, Person, PersonKind

__all__ = [
    "LINK_COMMAND",
    "LINK_LIFETIME",
    "AccountLink",
    "AffinityBook",
    "AffinityChange",
    "AffinityEvent",
    "AffinityReason",
    "PeopleDirectory",
    "PeopleError",
    "Person",
    "PersonKind",
    "describe_affinity",
]
