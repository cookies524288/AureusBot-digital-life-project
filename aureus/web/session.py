"""网页通道：登录状态（路线图 1.10，D63 第 7 条）。

登录后给浏览器一个不透明的随机 token；数据库里只存它的 SHA-256，所以库被人看到也拿不到能用的 token。
每台设备各自一条记录、各自计时：有效期从最后一次活动算起；只读检修不续期。
表 web_sessions 只属于网页通道，放在 aureus.db 里；它是登录状态，不是审计记录，可以删改。
"""

import asyncio
import hashlib
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aureus.common import Clock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import open_connection

# token 的随机字节数：256 位，猜不出来。
TOKEN_BYTES = 32
# 收到的 token 超过这个长度直接当作无效，不去查库。
MAX_TOKEN_LENGTH = 256
# 同一 token 的活动时间最多每 30 秒写一次库；每次请求都写会把心跳和网页互相拖住（D121）。
TOUCH_WRITE_INTERVAL = timedelta(seconds=30)


@dataclass
class _SessionTouch:
    """内存里的登录活动；有效期按这里的最近活动判断，数据库只是定期跟上。"""

    session_id: int
    last_active: datetime
    last_written: datetime


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    """登录状态的存取。"""

    def __init__(self, db_path: Path, clock: Clock, session_days: int) -> None:
        self._db_path = db_path
        self._clock = clock
        self._lifetime = timedelta(days=session_days)
        self._conn: sqlite3.Connection | None = None
        self._touches: dict[str, _SessionTouch] = {}

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
        self._touches.clear()
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
        digest = _hash_token(token)
        now = self._now()
        moment = now.isoformat(timespec="microseconds")
        with conn:
            cursor = conn.execute(
                "INSERT INTO web_sessions (token_hash, created_at, last_active_at) "
                "VALUES (?, ?, ?)",
                (digest, moment, moment),
            )
        # 这次 INSERT 就是最近一次写入，刚登录的 30 秒内不必再写。
        self._touches[digest] = _SessionTouch(int(cursor.lastrowid or 0), now, now)
        return token

    def _session_row(self, token: str) -> sqlite3.Row | None:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        connection = self._require_open()
        return connection.execute(
            "SELECT id, last_active_at FROM web_sessions WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()

    def is_active(self, token: str) -> bool:
        """只核对登录有效期，不续期、不清理过期状态。"""
        row = self._session_row(token)
        return (
            row is not None
            and self._now() - datetime.fromisoformat(row["last_active_at"]) < self._lifetime
        )

    def _read_session(self, digest: str) -> tuple[int, datetime] | None:
        """在工作线程里用独立连接读登录记录，不碰事件循环的数据库连接。"""
        with closing(open_connection(self._db_path, read_only=True)) as connection:
            row = connection.execute(
                "SELECT id, last_active_at FROM web_sessions WHERE token_hash = ?", (digest,)
            ).fetchone()
        if row is None:
            return None
        return int(row["id"]), datetime.fromisoformat(row["last_active_at"])

    def _write_last_active(self, session_id: int, moment: datetime) -> None:
        """在工作线程里用独立连接续期；等锁最多 1 秒，不会卡住事件循环。"""
        with closing(open_connection(self._db_path)) as connection, connection:
            connection.execute(
                "UPDATE web_sessions SET last_active_at = ? WHERE id = ?",
                (moment.isoformat(timespec="microseconds"), session_id),
            )

    def _delete_session(self, session_id: int) -> None:
        """过期时在工作线程里删掉记录。"""
        with closing(open_connection(self._db_path)) as connection, connection:
            connection.execute("DELETE FROM web_sessions WHERE id = ?", (session_id,))

    async def touch(self, token: str) -> bool:
        """这个 token 现在还有效吗？有效就续期，过期了就删掉。

        有效期按内存里的最近活动判断；数据库最多每 30 秒写一次，写库在工作线程（D121）。
        """
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        self._require_open()
        digest = _hash_token(token)
        now = self._now()
        state = self._touches.get(digest)
        if state is None:
            found = await asyncio.to_thread(self._read_session, digest)
            if found is None:
                return False
            identifier, last_active = found
            state = self._touches[digest] = _SessionTouch(identifier, last_active, last_active)
        if now - state.last_active >= self._lifetime:
            self._touches.pop(digest, None)
            await asyncio.to_thread(self._delete_session, state.session_id)
            return False
        state.last_active = now
        if now - state.last_written >= TOUCH_WRITE_INTERVAL:
            # 先记下写入时间再进线程：并发请求不会在同一窗口里重复写。
            state.last_written = now
            await asyncio.to_thread(self._write_last_active, state.session_id, now)
        return True

    def revoke(self, token: str) -> None:
        """让这个 token 失效（退出这一台设备）。"""
        digest = _hash_token(token)
        conn = self._require_open()
        with conn:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (digest,))
        self._touches.pop(digest, None)

    def revoke_all(self) -> int:
        """让所有设备的登录失效（退出所有设备、改密码），返回删掉了几条。"""
        conn = self._require_open()
        with conn:
            removed = conn.execute("DELETE FROM web_sessions").rowcount
        self._touches.clear()
        return removed

    def purge_expired(self) -> int:
        """删掉已经过期的登录状态，返回删掉了几条。"""
        conn = self._require_open()
        cutoff = (self._now() - self._lifetime).isoformat(timespec="microseconds")
        with conn:
            return conn.execute(
                "DELETE FROM web_sessions WHERE last_active_at <= ?", (cutoff,)
            ).rowcount
