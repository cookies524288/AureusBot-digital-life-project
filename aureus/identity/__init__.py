"""身份子系统。

提供身份配置读取和单实例锁。
"""

from .editor import (
    IdentityEdit,
    IdentityEditError,
    IdentityEditor,
    IdentityEditResult,
)
from .history import (
    PERSONA_LAYERS,
    IdentityHistory,
    IdentityHistoryError,
    IdentityLayer,
    IdentityVersion,
    VersionSource,
)
from .models import (
    Facts,
    Identity,
    Persona,
    PersonaLocks,
    User,
    UserAccount,
)
from .service import (
    IdentityError,
    IdentityFileNotFoundError,
    IdentityService,
    IdentityValidationError,
    SingleInstanceLockError,
)

__all__ = [
    "PERSONA_LAYERS",
    "Facts",
    "Identity",
    "IdentityEdit",
    "IdentityEditError",
    "IdentityEditResult",
    "IdentityEditor",
    "IdentityError",
    "IdentityFileNotFoundError",
    "IdentityHistory",
    "IdentityHistoryError",
    "IdentityLayer",
    "IdentityService",
    "IdentityValidationError",
    "IdentityVersion",
    "Persona",
    "PersonaLocks",
    "SingleInstanceLockError",
    "User",
    "UserAccount",
    "VersionSource",
]
