"""网页通道的服务端部分：密码、登录状态、（以后）网页服务（路线图 1.10，D63）。

没有别的子系统依赖本包：它在最上层，由核心组装时接进去。
"""

from aureus.web.auth import (
    DEFAULT_PASSWORD,
    LoginLimiter,
    LoginLockedError,
    LoginRejectedError,
    LoginResult,
    PasswordPolicyError,
    PasswordStore,
    WebAuth,
    is_loopback,
)
from aureus.web.server import create_app
from aureus.web.session import SessionStore

__all__ = [
    "DEFAULT_PASSWORD",
    "LoginLimiter",
    "LoginLockedError",
    "LoginRejectedError",
    "LoginResult",
    "PasswordPolicyError",
    "PasswordStore",
    "SessionStore",
    "WebAuth",
    "create_app",
    "is_loopback",
]
