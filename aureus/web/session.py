"""网页通道：登录状态（路线图 1.10，D63 第 7 条）。

登录后给浏览器一个不透明的随机 token；数据库里只存它的 SHA-256，所以库被人看到也拿不到能用的 token。
每台设备各自一条记录、各自计时：有效期从最后一次活动算起，每次使用都续期。
表 web_sessions 只属于网页通道，放在 aureus.db 里；它是登录状态，不是审计记录，可以删改。
"""

import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aureus.common import Clock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import open_connection

# token 的随机字节数：256 位，猜不出来。
TOKEN_BYTES = 32
# 收到的 token 超过这个长度直接当作无效，不去查库。
MAX_TOKEN_LENGTH = 256


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    """登录状态的存取。"""

    def __init__(self, db_path: Path, clock: Clock, session_days: int) -> None:
        self._db_path = db_path
        self._clock = clock
        self._lifetime = timedelta(days=session_days)
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """打开数据库连接并建表。已经打开时什么也不做。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("登录状态的存储还没有打开")
        return self._conn

    def _now(self) -> datetime:
        # 统一存 UTC：同样的格式，字符串的先后就是时间的先后（purge_expired 靠这个）。
        return ensure_aware(self._clock.now()).astimezone(UTC)

    def create(self) -> str:
        """新建一个登录状态，返回给浏览器的 token（只此一次，库里没有明文）。"""
        conn = self._require_open()
        self.purge_expired()  # 顺手清理，让这张表不会无限增长
        token = secrets.token_urlsafe(TOKEN_BYTES)
        moment = self._now().isoformat(timespec="microseconds")
        with conn:
            conn.execute(
                "INSERT INTO web_sessions (token_hash, created_at, last_active_at) "
                "VALUES (?, ?, ?)",
                (_hash_token(token), moment, moment),
            )
        return token

    def touch(self, token: str) -> bool:
        """这个 token 现在还有效吗？有效就续期，过期了就删掉。"""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        conn = self._require_open()
        now = self._now()
        row = conn.execute(
            "SELECT id, last_active_at FROM web_sessions WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            return False
        with conn:
            if now - datetime.fromisoformat(row["last_active_at"]) >= self._lifetime:
                conn.execute("DELETE FROM web_sessions WHERE id = ?", (row["id"],))
                return False
            conn.execute(
                "UPDATE web_sessions SET last_active_at = ? WHERE id = ?",
                (now.isoformat(timespec="microseconds"), row["id"]),
            )
        return True

    def revoke(self, token: str) -> None:
        """让这个 token 失效（退出这一台设备）。"""
        conn = self._require_open()
        with conn:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_hash_token(token),))

    def revoke_all(self) -> int:
        """让所有设备的登录失效（退出所有设备、改口令），返回删掉了几条。"""
        conn = self._require_open()
        with conn:
            return conn.execute("DELETE FROM web_sessions").rowcount

    def purge_expired(self) -> int:
        """删掉已经过期的登录状态，返回删掉了几条。"""
        conn = self._require_open()
        cutoff = (self._now() - self._lifetime).isoformat(timespec="microseconds")
        with conn:
            return conn.execute(
                "DELETE FROM web_sessions WHERE last_active_at <= ?", (cutoff,)
            ).rowcount
