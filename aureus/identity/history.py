"""身份的版本历史（路线图 1.9，03 号文档「身份」一节，D62）。

可变人格、行为风格、表达风格三层的当前版本和全部历史存在这里。身份事实和不变人格只认
identity.toml，这里只存它们的快照，用来发现「用户改过了」。表 identity_versions 只属于身份子系统，
放在 aureus.db 里；只追加，触发器禁止修改、删除和 INSERT OR REPLACE（和时间线、账本一样）。

怎么发现用户改了配置文件：每一层拿 identity.toml 里的内容，和这一层「最近一次来自配置文件」的
版本比，而不是和当前版本比。这样它自己改过的层（Phase 2），只要配置文件没动，重启后还是它改的样子；
配置文件动了，用户的版本成为最新的一版。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from aureus.common import Clock
from aureus.common.clock import ensure_aware
from aureus.common.sqlite import open_connection

from .models import Identity, PersonaLocks

# 时间固定写成带微秒的 UTC，和账本一样：按字符串比较大小就等于按时间比较。
_TIMESTAMP_PRECISION = "microseconds"


class IdentityHistoryError(Exception):
    """身份版本历史出了问题。消息是中文。"""


class IdentityLayer(StrEnum):
    """身份里分开记版本的几部分。"""

    FACTS = "facts"  # 身份事实：名字、昵称、性别、生日、用户的称呼和账号
    IMMUTABLE = "immutable"
    MUTABLE = "mutable"
    BEHAVIOR_STYLE = "behavior_style"
    EXPRESSION_STYLE = "expression_style"

    def describe(self) -> str:
        """中文名（09 号文档的术语）。"""
        return _LAYER_NAMES[self]


_LAYER_NAMES = {
    IdentityLayer.FACTS: "身份事实",
    IdentityLayer.IMMUTABLE: "不变人格",
    IdentityLayer.MUTABLE: "可变人格",
    IdentityLayer.BEHAVIOR_STYLE: "行为风格",
    IdentityLayer.EXPRESSION_STYLE: "表达风格",
}

# 存进数据库、以后它自己能改的三层；另外两层只认 identity.toml。
PERSONA_LAYERS = (
    IdentityLayer.MUTABLE,
    IdentityLayer.BEHAVIOR_STYLE,
    IdentityLayer.EXPRESSION_STYLE,
)


class VersionSource(StrEnum):
    """一个版本是从哪来的。"""

    INITIAL = "initial"  # 第一次启动时从 identity.toml 读进来的，不算「用户修改」
    USER_FILE = "user_file"  # 用户改了 identity.toml，启动时记成一次用户修改
    SELF = "self"  # 它自己改的（Phase 2 的睡眠整理）；锁定的层不能有这种版本

    def describe(self) -> str:
        """给人看的中文说法。"""
        return _SOURCE_NAMES[self]


_SOURCE_NAMES = {
    VersionSource.INITIAL: "初始版本",
    VersionSource.USER_FILE: "用户修改",
    VersionSource.SELF: "它自己改的",
}


_FILE_SOURCES = (VersionSource.INITIAL.value, VersionSource.USER_FILE.value)


@dataclass(frozen=True)
class IdentityVersion:
    """某一层的一个版本。"""

    id: int
    layer: IdentityLayer
    content: str
    source: VersionSource
    created_at: datetime


def file_contents(identity: Identity) -> dict[IdentityLayer, str]:
    """identity.toml 里每一层的内容，去掉首尾空白：多一个空行不算改了设定。

    身份事实写成键排好序的 JSON，账号也排好序：只调换顺序不算改了。
    """
    facts = identity.facts.model_dump(mode="json")
    user = identity.user.model_dump(mode="json")
    user["accounts"] = sorted(user["accounts"], key=lambda a: (a["platform"], a["id"]))
    persona = identity.persona
    return {
        IdentityLayer.FACTS: json.dumps(
            {"facts": facts, "user": user}, ensure_ascii=False, sort_keys=True
        ),
        IdentityLayer.IMMUTABLE: persona.immutable.strip(),
        IdentityLayer.MUTABLE: persona.mutable.strip(),
        IdentityLayer.BEHAVIOR_STYLE: persona.behavior_style.strip(),
        IdentityLayer.EXPRESSION_STYLE: persona.expression_style.strip(),
    }


class IdentityHistory:
    """身份的版本历史：启动时和 identity.toml 对一遍，给出此刻生效的身份。"""

    def __init__(self, db_path: Path, clock: Clock) -> None:
        self._db_path = db_path
        self._clock = clock
        self._conn: sqlite3.Connection | None = None
        # 最近一次 sync_from_file 读到的锁定状态；它自己改设定前要看。
        self._locks: PersonaLocks | None = None

    def open(self) -> None:
        """打开数据库连接并建表。已经打开时什么也不做。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            self._init_schema(conn)
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
            raise RuntimeError("身份版本历史还没有打开")
        return self._conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        layers = ", ".join(f"'{layer.value}'" for layer in IdentityLayer)
        sources = ", ".join(f"'{source.value}'" for source in VersionSource)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS identity_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL CHECK (layer IN ({layers})),
                content TEXT NOT NULL,
                source TEXT NOT NULL CHECK (source IN ({sources})),
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_identity_versions_layer ON identity_versions(layer, id)"
        )
        for action in ("UPDATE", "DELETE"):
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS no_{action.lower()}_identity_versions
                BEFORE {action} ON identity_versions
                BEGIN
                    SELECT RAISE(ABORT, 'Identity versions are immutable: {action} is not allowed');
                END
                """
            )
        # INSERT OR REPLACE 会先删掉冲突的行，默认不触发 DELETE 触发器，
        # 所以单独拦（同账本，审查 S3）。
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS no_replace_identity_versions
            BEFORE INSERT ON identity_versions
            WHEN EXISTS (SELECT 1 FROM identity_versions WHERE id = NEW.id)
            BEGIN
                SELECT RAISE(ABORT, 'Identity versions are immutable: REPLACE is not allowed');
            END
            """
        )
        conn.commit()

    def _insert(
        self, conn: sqlite3.Connection, layer: IdentityLayer, content: str, source: VersionSource
    ) -> None:
        moment = ensure_aware(self._clock.now()).isoformat(timespec=_TIMESTAMP_PRECISION)
        conn.execute(
            "INSERT INTO identity_versions (layer, content, source, created_at) "
            "VALUES (?, ?, ?, ?)",
            (layer.value, content, source.value, moment),
        )

    @staticmethod
    def _latest_content(
        conn: sqlite3.Connection, layer: IdentityLayer, *, from_file: bool
    ) -> str | None:
        """这一层最新版本的内容；from_file 为真时只看来自配置文件的版本。没有版本时是 None。"""
        if from_file:
            row = conn.execute(
                "SELECT content FROM identity_versions WHERE layer = ? AND source IN (?, ?) "
                "ORDER BY id DESC LIMIT 1",
                (layer.value, *_FILE_SOURCES),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT content FROM identity_versions WHERE layer = ? ORDER BY id DESC LIMIT 1",
                (layer.value,),
            ).fetchone()
        return None if row is None else str(row["content"])

    def sync_from_file(self, from_file: Identity) -> Identity:
        """和 identity.toml 对一遍：第一次见到的层记成初始版本，改过的层记成一次用户修改。

        Args:
            from_file: 从 identity.toml 读出来的身份。

        Returns:
            此刻生效的身份：身份事实、不变人格和锁定状态照 identity.toml；
            另外三层取数据库里的最新版本。

        Raises:
            RuntimeError: 还没有打开。
            sqlite3.Error: 读写失败，这次一条都没有写进去。
        """
        conn = self._require_open()
        contents = file_contents(from_file)
        # `with conn` 成功时提交、出错时回滚：要么每一层都对完，要么一条都不记。
        with conn:
            for layer, content in contents.items():
                previous = self._latest_content(conn, layer, from_file=True)
                if previous is None:
                    self._insert(conn, layer, content, VersionSource.INITIAL)
                elif previous != content:
                    self._insert(conn, layer, content, VersionSource.USER_FILE)
            current: dict[str, str] = {}
            for layer in PERSONA_LAYERS:
                latest = self._latest_content(conn, layer, from_file=False)
                if latest is not None:
                    current[layer.value] = latest
        self._locks = from_file.persona.locks
        persona = from_file.persona.model_copy(update=current)
        return from_file.model_copy(update={"persona": persona})

    def record_self_change(self, layer: IdentityLayer, content: str) -> None:
        """它自己改了一层设定（Phase 2 的睡眠整理用）。锁定的层不能改。

        Raises:
            IdentityHistoryError: 这一层不能由它自己改：身份事实、不变人格、被用户锁定的层；
                或者还没和 identity.toml 对过，不知道锁定状态（朝安全方向倒：不改）。
            RuntimeError: 还没有打开。
        """
        conn = self._require_open()
        if layer not in PERSONA_LAYERS:
            raise IdentityHistoryError(f"{layer.describe()}只有用户能改。")
        if self._locks is None:
            raise IdentityHistoryError("还不知道哪些层被锁定了，先不改。")
        if getattr(self._locks, layer.value):
            raise IdentityHistoryError(f"{layer.describe()}被用户锁定了，不能改。")
        with conn:
            self._insert(conn, layer, content.strip(), VersionSource.SELF)

    def versions(self, layer: IdentityLayer | None = None, *, limit: int) -> list[IdentityVersion]:
        """最新的几个版本，从早到晚；不给 layer 就是所有层混在一起。"""
        conn = self._require_open()
        if layer is None:
            rows = conn.execute(
                "SELECT id, layer, content, source, created_at FROM identity_versions "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, layer, content, source, created_at FROM identity_versions "
                "WHERE layer = ? ORDER BY id DESC LIMIT ?",
                (layer.value, limit),
            ).fetchall()
        return [_version_of(row) for row in reversed(rows)]

    def user_changes_after(self, version_id: int) -> list[IdentityVersion]:
        """序号在 version_id 之后的用户修改，从早到晚。"""
        conn = self._require_open()
        rows = conn.execute(
            "SELECT id, layer, content, source, created_at FROM identity_versions "
            "WHERE id > ? AND source = ? ORDER BY id",
            (version_id, VersionSource.USER_FILE.value),
        ).fetchall()
        return [_version_of(row) for row in rows]


def _version_of(row: sqlite3.Row) -> IdentityVersion:
    return IdentityVersion(
        id=int(row["id"]),
        layer=IdentityLayer(row["layer"]),
        content=str(row["content"]),
        source=VersionSource(row["source"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
