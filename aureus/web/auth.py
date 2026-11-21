"""网页通道：密码、登录限流和登录流程（路线图 1.10，D63 第 6、7 条）。

密码只存 argon2 哈希，放在 config/web_password.argon2。
没有这个文件就是「默认密码」状态，密码是 admin。
删掉这个文件等于把密码重置回默认：能碰到数据根目录的人本来就能这么做，
这也是忘了密码时的恢复办法。
默认密码人人都知道，所以默认状态下只认来自本机的登录。
"""

import hmac
import ipaddress
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from aureus.common import Clock, WebConfig
from aureus.web.session import SessionStore

DEFAULT_PASSWORD = "admin"
MIN_PASSWORD_LENGTH = 8
# 上限：argon2 要把整个密码读进内存算哈希，不设上限的话，一个超大的请求就能拖慢服务。
MAX_PASSWORD_LENGTH = 256


class PasswordPolicyError(ValueError):
    """新密码不合规。消息是中文，不回显密码。"""


class LoginRejectedError(Exception):
    """密码不对，或者当前状态不允许从这个地址登录。两种情况说法一样，不给猜密码的人线索。"""


class LoginLockedError(Exception):
    """这个 IP 登录失败太多次，正在锁定期里。"""

    def __init__(self, retry_after: timedelta) -> None:
        super().__init__("登录失败的次数太多，请稍后再试。")
        self.retry_after = retry_after


def check_new_password(password: str) -> None:
    """检查新密码是否合规（D63 第 6 条：至少 8 位，不能是默认密码）。"""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"密码至少要 {MIN_PASSWORD_LENGTH} 位。")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"密码最多 {MAX_PASSWORD_LENGTH} 位。")
    if password.strip().lower() == DEFAULT_PASSWORD:
        raise PasswordPolicyError("新密码不能是默认密码。")


def is_loopback(address: str) -> bool:
    """这个来源地址是不是本机。认不出来的地址当作不是（朝安全方向倒）。"""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


class PasswordStore:
    """密码哈希文件的读写。"""

    def __init__(self, path: Path, hasher: PasswordHasher | None = None) -> None:
        self._path = path
        self._hasher = hasher or PasswordHasher()

    def is_default(self) -> bool:
        """还在用默认密码吗？只有文件确实不存在才算。"""
        try:
            self._path.stat()
        except FileNotFoundError:
            return True
        except OSError:
            # 读不了文件：当作已经设过密码，之后的验证会失败，而不是放行默认密码。
            return False
        return False

    def verify(self, password: str) -> bool:
        """密码对不对。文件读不了、内容坏了都算不对。"""
        if len(password) > MAX_PASSWORD_LENGTH:
            return False
        if self.is_default():
            # 常量时间比较，和哈希的验证一样不泄露「对了几位」。
            return hmac.compare_digest(password.encode("utf-8"), DEFAULT_PASSWORD.encode("utf-8"))
        try:
            stored = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        try:
            return self._hasher.verify(stored, password)
        except (VerificationError, InvalidHashError):
            return False

    def set_password(self, new_password: str) -> None:
        """换成新密码：先检查是否合规，再写临时文件、替换，不会留下写一半的文件。

        Raises:
            PasswordPolicyError: 新密码不合规。
            OSError: 写不进去，旧密码保持不变。
        """
        check_new_password(new_password)
        digest = self._hasher.hash(new_password)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=".web_password-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(digest + "\n")
            os.replace(temp_name, self._path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


class LoginLimiter:
    """登录失败限流，按来源 IP 分开算，状态只放内存里（重启清零）。"""

    def __init__(self, config: WebConfig, clock: Clock) -> None:
        self._max_failures = config.login_max_failures
        self._window = timedelta(seconds=config.login_window_seconds)
        self._lockout = timedelta(seconds=config.login_lockout_seconds)
        self._clock = clock
        self._failures: dict[str, list[datetime]] = {}
        self._locked_until: dict[str, datetime] = {}

    def _prune(self, now: datetime) -> None:
        # 清掉过期的记录，免得换着 IP 试的人把内存撑大。
        self._locked_until = {k: t for k, t in self._locked_until.items() if t > now}
        kept = {k: [t for t in v if now - t < self._window] for k, v in self._failures.items()}
        self._failures = {k: v for k, v in kept.items() if v}

    def locked_for(self, key: str) -> timedelta | None:
        """这个 IP 还要被锁多久；没被锁返回 None。"""
        now = self._clock.now()
        self._prune(now)
        until = self._locked_until.get(key)
        return None if until is None else until - now

    def record_failure(self, key: str) -> None:
        """记一次失败；窗口内失败够了次数，就开始锁定。"""
        now = self._clock.now()
        self._prune(now)
        attempts = self._failures.setdefault(key, [])
        attempts.append(now)
        if len(attempts) >= self._max_failures:
            self._locked_until[key] = now + self._lockout
            del self._failures[key]

    def record_success(self, key: str) -> None:
        """登录成功：这个 IP 的失败记录清零。"""
        self._failures.pop(key, None)


@dataclass(frozen=True)
class LoginResult:
    """登录成功的结果。"""

    token: str
    must_change_password: bool  # 还在用默认密码：只能进改密码页（D63 第 6 条）


class WebAuth:
    """登录流程：限流、验密码、发 token、改密码。"""

    def __init__(
        self, passwords: PasswordStore, sessions: SessionStore, limiter: LoginLimiter
    ) -> None:
        self._passwords = passwords
        self._sessions = sessions
        self._limiter = limiter

    @property
    def password_is_default(self) -> bool:
        """还在用默认密码吗（D63 第 6 条：这时只能进改密码页）。"""
        return self._passwords.is_default()

    def confirm_password(self, password: str, remote_addr: str) -> None:
        """核对密码：登录、改密码、改身份事实和不变人格前都要过这一关（D63 第 1 条）。

        Raises:
            LoginLockedError: 这个 IP 正在锁定期。
            LoginRejectedError: 密码不对，或者默认密码却不是从本机来的。
        """
        retry_after = self._limiter.locked_for(remote_addr)
        if retry_after is not None:
            raise LoginLockedError(retry_after)
        matches = self._passwords.verify(password)
        # 默认密码人人都知道：不是本机就不认，免得局域网里的人用 admin 进来。
        allowed = not self._passwords.is_default() or is_loopback(remote_addr)
        if not (matches and allowed):
            self._limiter.record_failure(remote_addr)
            raise LoginRejectedError("密码不对。")
        self._limiter.record_success(remote_addr)

    def login(self, password: str, remote_addr: str) -> LoginResult:
        """登录，成功返回新 token。错误同 confirm_password。"""
        self.confirm_password(password, remote_addr)
        must_change = self._passwords.is_default()
        return LoginResult(token=self._sessions.create(), must_change_password=must_change)

    def change_password(self, old_password: str, new_password: str, remote_addr: str) -> None:
        """改密码：要输旧密码；改完所有设备的登录全部失效（D63 第 7 条）。

        Raises:
            LoginLockedError、LoginRejectedError: 旧密码没通过。
            PasswordPolicyError: 新密码不合规，旧密码和登录状态都不动。
        """
        self.confirm_password(old_password, remote_addr)
        self._passwords.set_password(new_password)
        self._sessions.revoke_all()
