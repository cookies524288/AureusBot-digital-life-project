"""人际 people：用已登录网页和 QQ 私聊验证账号控制权"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from aureus.common import Clock

from .directory import PeopleDirectory, PeopleError

LINK_COMMAND = "/aureus-link"
LINK_LIFETIME = timedelta(minutes=5)


@dataclass
class _Challenge:
    session: str
    account_id: str
    code: str
    expires_at: datetime
    original_contact: int | None
    verified: bool = False


class AccountLink:
    """一次只保留一个验证请求，由发起请求的网页会话最终确认"""

    def __init__(
        self,
        directory: PeopleDirectory,
        clock: Clock,
        web_account: tuple[str, str],
        platform: str,
        bot_account_id: str,
    ) -> None:
        self._directory = directory
        self._clock = clock
        self._web_account = web_account
        self._platform = platform
        self._bot_account_id = bot_account_id
        self._pending: _Challenge | None = None

    def issue(self, session: str, account_id: str) -> str:
        """生成指定 QQ 账号可用的短时验证码"""
        if account_id == self._bot_account_id:
            raise PeopleError("不能绑定机器人自己的账号。")
        contact = self._directory.imported_contact(self._platform, account_id)
        if contact is None and self._directory.find(self._platform, account_id) is not None:
            raise PeopleError("这个 QQ 账号已经有档案，无法自动关联。")
        code = secrets.token_urlsafe(16)
        self._pending = _Challenge(
            session, account_id, code, self._clock.now() + LINK_LIFETIME, contact
        )
        return code

    def receive(self, account_id: str, text: str, *, is_direct: bool) -> bool:
        """消化验证码消息，让它不进入经历、模型上下文和聊天回复"""
        if LINK_COMMAND not in text:
            return False
        pending = self._active()
        parts = text.split()
        if (
            is_direct
            and pending is not None
            and not pending.verified
            and account_id == pending.account_id
            and len(parts) == 2
            and parts[0] == LINK_COMMAND
            and parts[1].isascii()
            and secrets.compare_digest(parts[1], pending.code)
        ):
            pending.verified = True
        return True

    def confirm(self, session: str) -> str:
        """由发起绑定的有效会话确认后写入账号映射，返回已绑定账号"""
        pending = self._active()
        if pending is None or pending.session != session or not pending.verified:
            raise PeopleError("当前会话没有通过 QQ 验证的绑定请求。")
        user = self._directory.find(*self._web_account)
        if user is None or not user.is_user:
            raise PeopleError("当前网页登录账号无法确认用户身份。")
        self._directory.bind_verified_account(
            user, self._platform, pending.account_id, pending.original_contact
        )
        self._pending = None
        return pending.account_id

    def revoke(self, session: str) -> None:
        """撤销当前网页登录会话发起的请求"""
        if self._pending is not None and self._pending.session == session:
            self._pending = None

    def _active(self) -> _Challenge | None:
        if self._pending is not None and self._clock.now() >= self._pending.expires_at:
            self._pending = None
        return self._pending
