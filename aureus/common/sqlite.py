"""SQLite 连接的统一打开方式：时间线和账本都从这里拿连接。"""

import sqlite3
from pathlib import Path

# 被别的进程占着写锁时，最多等这么久就放弃（抛 sqlite3.OperationalError）。
# 心跳和器官调用都在事件循环里同步写库，等锁时整个循环停着；Python 默认等 5 秒，
# 会顶掉一整拍。1 秒够正常的短暂争用，等不到就当出错：心跳遇到写入失败会停下来，
# 而不是带着没记全的经历继续跑（审查 M2；写库挪出事件循环留到 Phase 1 接通道时一起做）。
SQLITE_BUSY_TIMEOUT_SECONDS = 1.0


def open_connection(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """打开一个数据库连接，等锁的时间有上限。

    Args:
        db_path: 数据库文件。父目录要已经存在。
        read_only: 只读打开已有数据库，不创建数据库。

    Returns:
        新的连接，行用 sqlite3.Row 表示。

    Raises:
        sqlite3.Error: 打不开文件。
    """
    connection = sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro" if read_only else str(db_path),
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        check_same_thread=False,
        uri=read_only,
    )
    connection.row_factory = sqlite3.Row
    return connection


def enable_wal(connection: sqlite3.Connection) -> None:
    """把数据库切到 WAL 模式（读写不互斥），并核对真的生效了。

    设置存在数据库文件里，之后打开的连接自动沿用。切不过去时抛错，让启动失败：
    悄悄退回旧模式会在下次卡顿时才发现（D121：宁可启动失败也不假装成功）。

    Args:
        connection: 可写连接。

    Raises:
        sqlite3.Error: 切换失败，或者切换后读回来的不是 wal。
    """
    row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    mode = row[0] if row is not None else None
    if str(mode).lower() != "wal":
        raise sqlite3.OperationalError(f"数据库没有切换到 WAL 模式（当前为 {mode!r}）")
