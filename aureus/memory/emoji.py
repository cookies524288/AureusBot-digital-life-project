"""按内容哈希维护学习页和回复流程使用的表情库。"""

import json
import re
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from sqlite3 import Connection, Row
from tempfile import NamedTemporaryFile
from typing import Literal

from PIL import Image, ImageSequence

from aureus.common import Clock
from aureus.common.config import DEFAULT_EMOJI_REGISTRATION_LIMIT
from aureus.common.sqlite import open_connection
from aureus.memory.history import MemorySource
from aureus.memory.learning_workbench_ids import identify as identify_workbench_ids
from aureus.memory.learning_workbench_ids import initialize as initialize_workbench_ids
from aureus.memory.learning_workbench_ids import resolve as resolve_workbench_ids

_DIGEST = re.compile(r"[a-f0-9]{64}")
MAX_EMOJI_BYTES = 6 * 1024 * 1024
MAX_EMOJI_DESCRIPTION = 500
MAX_EMOJI_RECOGNITION_TAGS = 5
MAX_EMOJI_TAGS = 20
MAX_EMOJI_TAG = 40
_MAX_FRAME_PIXELS = 4_000_000
_MAX_TOTAL_PIXELS = 64_000_000
_MAX_FRAMES = 200
_UPLOAD_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
type EmojiStatus = Literal["", "registered", "unregistered", "banned"]
type EmojiReview = Literal["unreviewed", "approved", "rejected"]
type EmojiVerdict = Literal["approved", "rejected"]
type EmojiFormat = Literal["", "png", "jpeg", "gif", "webp"]
type EmojiSort = Literal["recent", "most_used", "most_collected", "last_used"]
_SORT_COLUMNS: dict[EmojiSort, str] = {
    "recent": "first_collected_at",
    "most_used": "use_count",
    "most_collected": "collection_count",
    "last_used": "last_used_at",
}
_REVIEW = """coalesce((SELECT verdict FROM emoji_reviews
    WHERE emoji_reviews.digest = emoji_assets.digest), 'unreviewed')"""
_SENDABLE = f"""is_registered AND NOT is_banned AND {_REVIEW} <> 'rejected' AND NOT EXISTS (
    SELECT 1 FROM emoji_removed WHERE entry_key = 'asset:' || digest)"""
_ACTIVE = """is_registered AND NOT is_banned AND NOT EXISTS (
    SELECT 1 FROM emoji_removed WHERE entry_key = 'asset:' || digest)"""
_CATALOG = f"""WITH entries AS (
    SELECT emoji_assets.*, (SELECT min(source_key) FROM emoji_sources source
        WHERE source.digest = emoji_assets.digest) AS source, {_REVIEW} AS review
    FROM emoji_assets
    UNION ALL
    SELECT NULL, coalesce(json_extract(original, '$.description'), ''), '[]',
        coalesce(json_extract(original, '$.is_registered') IN (1, '1', 'True'), 0),
        coalesce(json_extract(original, '$.is_banned') IN (1, '1', 'True'), 0), 1,
        max(0, coalesce(CAST(json_extract(original, '$.query_count') AS INTEGER), 0)),
        NULL, NULL, source_key, 'unreviewed'
    FROM emoji_sources WHERE digest IS NULL
), catalog AS (
    SELECT *, EXISTS (SELECT 1 FROM emoji_removed WHERE entry_key =
        coalesce('asset:' || digest, 'source:' || source)) AS is_removed
    FROM entries
)"""
_SCHEMA = """
CREATE TABLE IF NOT EXISTS emoji_assets (
    digest TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    tags TEXT NOT NULL,
    is_registered INTEGER NOT NULL,
    is_banned INTEGER NOT NULL,
    collection_count INTEGER NOT NULL,
    use_count INTEGER NOT NULL,
    first_collected_at REAL,
    last_used_at REAL
);
CREATE TABLE IF NOT EXISTS emoji_sources (
    source_key TEXT PRIMARY KEY,
    digest TEXT REFERENCES emoji_assets(digest),
    original TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS emoji_sources_digest ON emoji_sources(digest);
CREATE TABLE IF NOT EXISTS emoji_removed (entry_key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS emoji_reviews (
    digest TEXT PRIMARY KEY REFERENCES emoji_assets(digest),
    verdict TEXT NOT NULL CHECK (verdict IN ('approved', 'rejected'))
);
CREATE TABLE IF NOT EXISTS emoji_registration_versions (
    digest TEXT PRIMARY KEY REFERENCES emoji_assets(digest), version INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS emoji_registration_update
AFTER UPDATE OF description, tags, is_registered, is_banned ON emoji_assets
WHEN (OLD.description, OLD.tags, OLD.is_registered, OLD.is_banned) IS NOT
     (NEW.description, NEW.tags, NEW.is_registered, NEW.is_banned)
BEGIN
    INSERT INTO emoji_registration_versions VALUES (NEW.digest, 1)
    ON CONFLICT(digest) DO UPDATE SET version = version + 1;
END;
"""


def _version(connection: Connection, digest: str) -> None:
    connection.execute(
        """INSERT INTO emoji_registration_versions VALUES (?, 1)
           ON CONFLICT(digest) DO UPDATE SET version = version + 1""",
        (digest,),
    )


def _active_count(connection: Connection) -> int:
    return int(
        connection.execute(f"SELECT count(*) FROM emoji_assets WHERE {_ACTIVE}").fetchone()[0]
    )


@dataclass(frozen=True)
class EmojiRecord:
    """一份表情媒体及其当前登记、使用和可读状态。"""

    digest: str | None
    description: str
    tags: tuple[str, ...]
    is_registered: bool
    is_banned: bool
    collection_count: int
    use_count: int
    has_file: bool
    source: str | None = None
    is_removed: bool = False
    media_type: str | None = None
    review: EmojiReview = "unreviewed"
    collected_at: float | None = None
    last_used_at: float | None = None

    @property
    def entry_key(self) -> str:
        """有摘要时定位媒体，否则定位保留的迁入原件。"""
        return f"asset:{self.digest}" if self.digest else f"source:{self.source}"


@dataclass(frozen=True)
class EmojiIntegrityPreview:
    """当前活跃登记条目的原件检查统计与问题示例。"""

    checked: int
    unavailable: int
    examples: tuple[str, ...]


@dataclass(frozen=True)
class EmojiReplacement:
    """指定新旧内容地址及当前可审核的登记版本，不携带文件路径。"""

    incoming: str
    outgoing: str
    incoming_version: int
    outgoing_version: int
    incoming_description: str
    outgoing_description: str


@dataclass(frozen=True)
class EmojiMaterial:
    """一份经过预检的素材及其来源声明；不带登记或审批权限。"""

    digest: str
    data: bytes
    description: str
    source_license: str


@dataclass(frozen=True)
class ImportedEmoji:
    """一条旧表情记录的稳定来源、媒体地址及原始元数据。"""

    source: MemorySource
    digest: str | None
    description: str
    is_registered: bool
    is_banned: bool
    collection_count: int
    use_count: int
    collected_at: float | None
    last_used_at: float | None
    original: str


def emoji_media_type(data: bytes) -> str | None:
    """识别允许作为表情浏览和发送的图片格式。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate_image(data: bytes) -> None:
    if not data or len(data) > MAX_EMOJI_BYTES or emoji_media_type(data) is None:
        raise ValueError("请选择不超过 5 MB 的 PNG、JPEG、GIF 或 WebP 图片。")
    try:
        with Image.open(BytesIO(data), formats=_UPLOAD_FORMATS) as image:
            if image.width * image.height > _MAX_FRAME_PIXELS:
                raise ValueError("图片尺寸或动画帧数过大。")
            image.verify()
        with Image.open(BytesIO(data), formats=_UPLOAD_FORMATS) as image:
            total_pixels = 0
            for number, frame in enumerate(ImageSequence.Iterator(image), 1):
                pixels = frame.width * frame.height
                total_pixels += pixels
                if (
                    pixels > _MAX_FRAME_PIXELS
                    or total_pixels > _MAX_TOTAL_PIXELS
                    or number > _MAX_FRAMES
                ):
                    raise ValueError("图片尺寸或动画帧数过大。")
                frame.load()
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("图片损坏或无法完整读取。") from None


_REGISTRATION_DESCRIPTION_REQUIRED = "请先填写表情描述，再登记或恢复使用。"


def _clean_description(description: str) -> str:
    description = description.strip()
    if len(description) > MAX_EMOJI_DESCRIPTION:
        raise ValueError("表情描述过长。")
    return description


class EmojiStore:
    """维护资源目录及内容寻址附件，各操作使用独立连接。"""

    def __init__(
        self,
        database_file: Path,
        memory_dir: Path,
        clock: Clock,
        *,
        registration_limit: int = DEFAULT_EMOJI_REGISTRATION_LIMIT,
    ) -> None:
        self._database_file = database_file
        self._memory_dir = memory_dir
        self._attachments = memory_dir / "attachments"
        self._clock = clock
        self.registration_limit = registration_limit
        self._unavailable_digests: frozenset[str] = frozenset()

    def open(self) -> None:
        """初始化表情资源与迁移来源表。"""
        with closing(open_connection(self._database_file)) as connection:
            connection.executescript(_SCHEMA)
            initialize_workbench_ids(connection)

    def workbench_ids(self, keys: Iterable[str]) -> dict[str, int]:
        """返回表情目录条目的稳定整数编号。"""
        with closing(open_connection(self._database_file)) as connection, connection:
            return identify_workbench_ids(connection, "emoji", keys)

    def resolve_workbench_ids(self, identifiers: Iterable[int]) -> dict[int, str]:
        """解析已分配的表情工作台编号。"""
        with closing(open_connection(self._database_file)) as connection:
            return resolve_workbench_ids(connection, "emoji", identifiers)

    def collect(
        self,
        data: bytes,
        *,
        description: str = "",
        tags: tuple[str, ...] = (),
        register: bool = False,
    ) -> EmojiRecord:
        """按内容收集图片，确认描述时可登记本次收集的表情。"""
        if emoji_media_type(data) is None:
            raise ValueError("该文件格式不能作为表情收集。")
        digest = sha256(data).hexdigest()
        self._write_media(digest, data)
        description = description.strip()
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            can_register = register and bool(description) and self._has_slot(connection)
            row = connection.execute(
                f"""INSERT INTO emoji_assets VALUES (?, ?, ?, ?, 0, 1, 0, ?, NULL)
                   ON CONFLICT(digest) DO UPDATE SET collection_count = collection_count + ?,
                       is_registered = is_registered OR (
                           excluded.is_registered AND NOT is_banned AND description = ''
                           AND {_REVIEW} <> 'rejected' AND NOT EXISTS (
                               SELECT 1 FROM emoji_removed WHERE entry_key = 'asset:' || ?)),
                       description = CASE WHEN description = '' THEN excluded.description
                                          ELSE description END,
                       tags = CASE WHEN tags = '[]' THEN excluded.tags ELSE tags END
                   RETURNING *, NULL AS source, {_REVIEW} AS review,
                       EXISTS (SELECT 1 FROM emoji_removed
                       WHERE entry_key = 'asset:' || ?) AS is_removed""",
                (
                    digest,
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    can_register,
                    self._clock.now().timestamp(),
                    int(not register),
                    digest,
                    digest,
                ),
            ).fetchone()
        return replace(self._record(row), is_removed=bool(row["is_removed"]))

    def register_recognition(self, digest: str, tags: tuple[str, ...]) -> bool:
        """只登记仍未维护的已收集条目，不重复计数或覆盖人工操作。"""
        if not 0 < len(tags) <= MAX_EMOJI_RECOGNITION_TAGS or any(
            not tag.strip() or len(tag) > MAX_EMOJI_TAG for tag in tags
        ):
            raise ValueError("表情标签数量或长度不可用。")
        description = "，".join(tags)
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            can_register = self._has_slot(connection)
            return bool(
                connection.execute(
                    f"""UPDATE emoji_assets SET description = ?, tags = ?, is_registered = ?
                       WHERE digest = ? AND NOT is_registered AND NOT is_banned
                         AND {_REVIEW} <> 'rejected'
                         AND description = '' AND tags = '[]' AND NOT EXISTS (
                             SELECT 1 FROM emoji_removed WHERE entry_key = 'asset:' || digest)""",
                    (description, json.dumps(tags, ensure_ascii=False), can_register, digest),
                ).rowcount
                and can_register
            )

    def import_records(self, records: Iterable[ImportedEmoji]) -> tuple[int, int]:
        """按来源增量登记，返回新增来源和内容地址数。"""
        source_count = asset_count = 0
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in records:
                source = row.source.encode()
                if connection.execute(
                    "SELECT 1 FROM emoji_sources WHERE source_key = ?", (source,)
                ).fetchone():
                    continue
                if row.digest is not None:
                    asset_count += connection.execute(
                        """INSERT INTO emoji_assets VALUES (?, ?, '[]', ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(digest) DO NOTHING""",
                        (
                            row.digest,
                            row.description,
                            row.is_registered,
                            row.is_banned,
                            row.collection_count,
                            row.use_count,
                            row.collected_at,
                            row.last_used_at,
                        ),
                    ).rowcount
                source_count += connection.execute(
                    "INSERT INTO emoji_sources VALUES (?, ?, ?)", (source, row.digest, row.original)
                ).rowcount
        return source_count, asset_count

    def upload(
        self,
        data: bytes,
        *,
        registration_description: str | None = None,
        tags: tuple[str, ...] = (),
        is_registered: bool | None = None,
    ) -> tuple[str, bool]:
        """存入经过检查的媒体，返回内容地址和是否新建；重复上传不改元数据。"""
        description = _clean_description(registration_description or "")
        tags = tuple(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
        if len(tags) > MAX_EMOJI_TAGS or any(len(tag) > MAX_EMOJI_TAG for tag in tags):
            raise ValueError("最多填写 20 个标签，每个不超过 40 字。")
        registered = (
            registration_description is not None
            if is_registered is None
            else is_registered
        )
        if registered and not description:
            raise ValueError(_REGISTRATION_DESCRIPTION_REQUIRED)
        _validate_image(data)
        digest = sha256(data).hexdigest()
        self._write_media(digest, data)
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            registered = registered and self._has_slot(connection)
            created = connection.execute(
                """INSERT INTO emoji_assets VALUES (?, ?, ?, ?, 0, 0, 0, ?, NULL)
                   ON CONFLICT(digest) DO NOTHING""",
                (
                    digest,
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    registered,
                    self._clock.now().timestamp(),
                ),
            ).rowcount
        return digest, bool(created)

    def update_catalog(
        self,
        digest: str,
        *,
        description: str | None = None,
        tags: tuple[str, ...] | None = None,
        is_registered: bool | None = None,
        is_banned: bool | None = None,
    ) -> bool:
        """修订当前目录字段，不改媒体、计数和迁移来源；禁用独立于登记。"""
        if description is not None:
            description = _clean_description(description)
        if tags is not None:
            tags = tuple(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
            if len(tags) > MAX_EMOJI_TAGS or any(len(tag) > MAX_EMOJI_TAG for tag in tags):
                raise ValueError("最多填写 20 个标签，每个不超过 40 字。")
        media_error = ""
        if is_registered is True or is_banned is False:
            data = self.read_media(digest)
            if data is None:
                media_error = "找不到可用原件，不能登记或恢复使用。"
            else:
                try:
                    _validate_image(data)
                except ValueError as error:
                    media_error = str(error)
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""SELECT *, {_REVIEW} AS review, EXISTS (
                    SELECT 1 FROM emoji_removed WHERE entry_key = 'asset:' || digest
                ) AS is_removed FROM emoji_assets WHERE digest = ?""",
                (digest,),
            ).fetchone()
            if row is None:
                return False
            new_description = row["description"] if description is None else description
            registered = bool(row["is_registered"]) if is_registered is None else is_registered
            banned = bool(row["is_banned"]) if is_banned is None else is_banned
            registering = registered and not row["is_registered"]
            restoring = registered and not banned and row["is_banned"]
            if (
                registered
                and (registering or restoring or description is not None)
                and not (new_description.strip())
            ):
                raise ValueError(_REGISTRATION_DESCRIPTION_REQUIRED)
            if (registering or restoring) and media_error:
                raise ValueError(media_error)
            if (registering or restoring) and not banned:
                if row["is_removed"] or row["review"] == "rejected":
                    raise ValueError("已移除或被拒绝的表情不能登记。")
                if not self._has_slot(connection):
                    raise ValueError("登记容量已满，请先取消旧登记或使用替换。")
            connection.execute(
                """UPDATE emoji_assets SET description = ?, tags = ?, is_registered = ?,
                   is_banned = ? WHERE digest = ?""",
                (
                    new_description,
                    row["tags"] if tags is None else json.dumps(tags, ensure_ascii=False),
                    registered,
                    banned,
                    digest,
                ),
            )
        return True

    def review_catalog(self, digest: str, verdict: EmojiVerdict) -> bool:
        """保存用户审核决定；拒绝不改变登记、禁用或移除，但会拦截发送。"""
        data = self.read_media(digest) if verdict == "approved" else None
        if data is not None:
            _validate_image(data)
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT description FROM emoji_assets WHERE digest = ? AND NOT EXISTS (
                       SELECT 1 FROM emoji_removed WHERE entry_key = 'asset:' || digest)""",
                (digest,),
            ).fetchone()
            if row is None:
                return False
            if verdict == "approved":
                if data is None:
                    raise ValueError("找不到可用原件，不能通过表情审批。")
                if not row["description"].strip():
                    raise ValueError("请先填写表情描述，再通过审批。")
            changed = connection.execute(
                """INSERT INTO emoji_reviews VALUES (?, ?)
                   ON CONFLICT(digest) DO UPDATE SET verdict = excluded.verdict
                   WHERE verdict <> excluded.verdict""",
                (digest, verdict),
            ).rowcount
            if changed:
                _version(connection, digest)
        return True

    def set_status(self, digest: str, *, is_registered: bool, is_banned: bool) -> None:
        """设置表情当前的登记、禁用状态。"""
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute(
                "UPDATE emoji_assets SET is_registered = ?, is_banned = ? WHERE digest = ?",
                (is_registered, is_banned, digest),
            )

    def record_use(self, digest: str) -> None:
        """在确认发送成功后累计一次实际使用。"""
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute(
                "UPDATE emoji_assets SET use_count = use_count + 1, last_used_at = ? "
                "WHERE digest = ?",
                (self._clock.now().timestamp(), digest),
            )

    def set_removed(self, keys: list[str], removed: bool) -> bool:
        """整批设置或撤销移除标记，保留媒体和登记、禁用状态。"""
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for key in keys:
                prefix, _, value = key.partition(":")
                if prefix == "asset":
                    query = "SELECT 1 FROM emoji_assets WHERE digest = ?"
                elif prefix == "source":
                    query = "SELECT 1 FROM emoji_sources WHERE source_key = ? AND digest IS NULL"
                else:
                    return False
                if connection.execute(query, (value,)).fetchone() is None:
                    return False
            statement = (
                "INSERT OR IGNORE INTO emoji_removed VALUES (?)"
                if removed
                else "DELETE FROM emoji_removed WHERE entry_key = ?"
            )
            if not removed:
                restored = sum(
                    connection.execute(
                        """SELECT EXISTS (SELECT 1 FROM emoji_assets
                           JOIN emoji_removed ON entry_key = 'asset:' || digest
                           WHERE entry_key = ? AND is_registered AND NOT is_banned)""",
                        (key,),
                    ).fetchone()[0]
                    for key in set(keys)
                )
                if restored and _active_count(connection) + restored > self.registration_limit:
                    raise ValueError("恢复后将超过表情登记容量，请先取消旧登记。")
            for key in dict.fromkeys(keys):
                if connection.execute(statement, (key,)).rowcount and key.startswith("asset:"):
                    _version(connection, key.removeprefix("asset:"))
        return True

    def _has_slot(self, connection: Connection) -> bool:
        return _active_count(connection) < self.registration_limit

    def registration_count(self) -> int:
        """返回参与容量计算的现有登记数，不改变历史数据。"""
        with closing(open_connection(self._database_file)) as connection:
            return _active_count(connection)

    def is_registered(self, digest: str) -> bool:
        """检查上传条目的实际登记结果。"""
        with closing(open_connection(self._database_file)) as connection:
            row = connection.execute(
                f"SELECT 1 FROM emoji_assets WHERE digest = ? AND {_ACTIVE}", (digest,)
            ).fetchone()
        return row is not None

    def export_materials(self, keys: list[str], source_license: str) -> list[EmojiMaterial]:
        """只取明示选中的未移除资产；每份原件必须可完整读取。"""
        materials = []
        with closing(open_connection(self._database_file)) as connection:
            connection.execute("BEGIN")
            for key in dict.fromkeys(keys):
                if not key.startswith("asset:"):
                    raise ValueError("没有原图的档案不能导出为素材。")
                digest = key.removeprefix("asset:")
                row = self._replacement_row(connection, digest)
                data = (
                    self.read_media(digest) if row is not None and not row["is_removed"] else None
                )
                if row is None or data is None:
                    raise ValueError("有选中的素材已移除或缺少原件。")
                _validate_image(data)
                materials.append(EmojiMaterial(digest, data, row["description"], source_license))
        return materials

    def import_materials(
        self, materials: list[EmojiMaterial], *, confirmed: bool, register: bool
    ) -> dict[str, int]:
        """按内容键去重，只登记已确认的新素材，返回预检或实际数量。"""
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE" if confirmed else "BEGIN")
            slots = max(0, self.registration_limit - _active_count(connection))
            added = registered = skipped = no_slot = no_description = 0
            for material in materials:
                if self._replacement_row(connection, material.digest) is not None:
                    skipped += 1
                    continue
                can_register = register and bool(material.description) and slots > 0
                if confirmed:
                    self._write_media(material.digest, material.data)
                    connection.execute(
                        "INSERT INTO emoji_assets VALUES (?, ?, '[]', ?, 0, 0, 0, ?, NULL)",
                        (
                            material.digest,
                            material.description,
                            can_register,
                            self._clock.now().timestamp(),
                        ),
                    )
                    source = MemorySource("aureus", "main", "emoji_materials", (material.digest,))
                    connection.execute(
                        "INSERT INTO emoji_sources VALUES (?, ?, ?)",
                        (
                            source.encode(),
                            material.digest,
                            json.dumps(
                                {"source_license": material.source_license}, ensure_ascii=False
                            ),
                        ),
                    )
                added += 1
                if can_register:
                    registered += 1
                    slots -= 1
                elif register and not material.description:
                    no_description += 1
                elif register:
                    no_slot += 1
        return {
            "added": added,
            "registered": registered,
            "skipped": skipped,
            "capacity_blocked": no_slot,
            "missing_description": no_description,
        }

    def replacement_options(self, incoming: str, offset: int, limit: int) -> list[EmojiReplacement]:
        """读取一页可撤销登记的替换对象，供预览或独立建议使用。"""
        with closing(open_connection(self._database_file)) as connection:
            connection.execute("BEGIN")
            target = self._replacement_row(connection, incoming)
            self._check_replacement_target(target)
            assert target is not None
            rows = connection.execute(
                f"""SELECT digest FROM emoji_assets WHERE {_ACTIVE}
                    ORDER BY use_count, last_used_at, digest LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            return [
                self._replacement(target, previous)
                for row in rows
                if (previous := self._replacement_row(connection, row["digest"])) is not None
            ]

    def confirm_replacement(
        self, incoming: str, outgoing: str, incoming_version: int, outgoing_version: int
    ) -> None:
        """确认旧登记撤销与新登记生效，原件及审批均不变。"""
        data = self.read_media(incoming)
        if data is None:
            raise ValueError("找不到可用原件，不能替换。")
        _validate_image(data)
        with closing(open_connection(self._database_file)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            target = self._replacement_row(connection, incoming)
            previous = self._replacement_row(connection, outgoing)
            self._check_replacement_target(target)
            if (
                target is None
                or previous is None
                or not previous["is_active"]
                or target["version"] != incoming_version
                or previous["version"] != outgoing_version
            ):
                raise ValueError("登记或审批已变更，请重新预览。")
            connection.execute(
                "UPDATE emoji_assets SET is_registered = 0 WHERE digest = ?", (outgoing,)
            )
            connection.execute(
                "UPDATE emoji_assets SET is_registered = 1 WHERE digest = ?", (incoming,)
            )

    @staticmethod
    def _replacement_row(connection: Connection, digest: str) -> Row | None:
        return connection.execute(
            f"""SELECT emoji_assets.*, {_ACTIVE} AS is_active, {_REVIEW} AS review,
                coalesce(version, 0) AS version, EXISTS (SELECT 1 FROM emoji_removed
                WHERE entry_key = 'asset:' || digest) AS is_removed
                FROM emoji_assets LEFT JOIN emoji_registration_versions USING (digest)
                WHERE digest = ?""",
            (digest,),
        ).fetchone()

    @staticmethod
    def _check_replacement_target(row: Row | None) -> None:
        if (
            row is None
            or row["is_registered"]
            or row["is_banned"]
            or row["is_removed"]
            or row["review"] == "rejected"
            or not row["description"].strip()
        ):
            raise ValueError("新表情须未登记、未禁用、未移除、未被拒绝且有描述。")

    @staticmethod
    def _replacement(target: Row, previous: Row) -> EmojiReplacement:
        return EmojiReplacement(
            target["digest"],
            previous["digest"],
            target["version"],
            previous["version"],
            target["description"],
            previous["description"],
        )

    def browse(
        self, query: str, offset: int, limit: int, status: EmojiStatus = ""
    ) -> list[EmojiRecord]:
        """按描述或标签浏览表情，包含停用与文件缺失的条目。"""
        rows, _total = self.browse_page(query, offset, limit, status)
        return rows

    def browse_page(
        self,
        query: str,
        offset: int,
        limit: int,
        status: EmojiStatus = "",
        *,
        removed: bool = False,
        image_format: EmojiFormat = "",
        sort: EmojiSort = "recent",
        review: EmojiReview | Literal[""] = "",
    ) -> tuple[list[EmojiRecord], int]:
        """返回筛选后的一页；指定格式时先核对媒体内容，再计算页码。"""
        filtered = f"""{_CATALOG}, filtered AS (SELECT * FROM catalog
            WHERE (instr(lower(description), lower(?)) > 0
                OR instr(lower(tags), lower(?)) > 0)
                AND is_removed = ? AND (? = '' OR review = ?)
                AND CASE ? WHEN 'registered' THEN is_registered AND NOT is_banned
                    WHEN 'unregistered' THEN NOT is_registered AND NOT is_banned
                    WHEN 'banned' THEN is_banned ELSE 1 END)"""
        params = (query, query, removed, review, review, status)
        order = f"ORDER BY {_SORT_COLUMNS[sort]} DESC, digest, source"
        with closing(open_connection(self._database_file)) as connection:
            connection.execute("BEGIN")
            if image_format:
                rows = connection.execute(f"{filtered} SELECT * FROM filtered {order}", params)
                entries = []
                total = 0
                for row in rows:
                    entry = self._catalog_record(row, removed)
                    if entry.media_type == f"image/{image_format}":
                        if offset <= total < offset + limit:
                            entries.append(entry)
                        total += 1
                return entries, total
            total = connection.execute(
                f"{filtered} SELECT count(*) FROM filtered", params
            ).fetchone()[0]
            rows = connection.execute(
                f"{filtered} SELECT * FROM filtered {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [self._catalog_record(row, removed) for row in rows], total

    def catalog_entry(self, entry_key: str, *, removed: bool = False) -> EmojiRecord | None:
        """按稳定目录键读取一条表情；不接收文件路径。"""
        with closing(open_connection(self._database_file)) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                f"""{_CATALOG}
                    SELECT * FROM catalog
                    WHERE coalesce('asset:' || digest, 'source:' || source) = ?
                      AND is_removed = ?""",
                (entry_key, removed),
            ).fetchall()
        if not rows:
            return None
        return self._catalog_record(rows[0], removed)

    def catalog_counts(self) -> dict[str, int]:
        """统计整个表情库，移除、禁用、登记和未登记互不重叠。"""
        with closing(open_connection(self._database_file)) as connection:
            row = connection.execute(
                f"""{_CATALOG} SELECT count(*) AS total,
                    count(CASE WHEN is_removed THEN 1 END) AS removed,
                    count(CASE WHEN NOT is_removed AND is_banned THEN 1 END) AS banned,
                    count(CASE WHEN NOT is_removed AND NOT is_banned AND is_registered
                        THEN 1 END) AS registered,
                    count(CASE WHEN NOT is_removed AND NOT is_banned AND NOT is_registered
                        THEN 1 END) AS unregistered
                    FROM catalog"""
            ).fetchone()
        return dict(row)

    def _catalog_record(self, row: Row, removed: bool) -> EmojiRecord:
        entry = self._record(row)
        data = (
            self.read_media(entry.digest) if entry.has_file and entry.digest is not None else None
        )
        media_type = emoji_media_type(data) if data is not None else None
        return replace(
            entry, has_file=media_type is not None, is_removed=removed, media_type=media_type
        )

    def preview_integrity(self, limit: int) -> EmojiIntegrityPreview:
        """核对活跃登记条目的原件，不改变登记状态或媒体。"""
        checked, unavailable = self._check_integrity()
        return EmojiIntegrityPreview(
            checked,
            len(unavailable),
            tuple(description for _, description in unavailable[: max(0, limit)]),
        )

    def refresh_sendable_pool(self) -> None:
        """按现有原件检查刷新内存排除项，不修改目录和文件。"""
        _, unavailable = self._check_integrity()
        self._unavailable_digests = frozenset(digest for digest, _ in unavailable if digest)

    def _check_integrity(self) -> tuple[int, list[tuple[str | None, str]]]:
        with closing(open_connection(self._database_file)) as connection:
            rows = connection.execute(
                f"""{_CATALOG} SELECT digest, description FROM catalog
                    WHERE is_registered AND NOT is_banned AND NOT is_removed
                    ORDER BY digest, source"""
            ).fetchall()
        unavailable = [
            (digest, row["description"])
            for row in rows
            if (digest := row["digest"]) is None or self.read_media(digest) is None
        ]
        return len(rows), unavailable

    def candidates(self) -> list[EmojiRecord]:
        """列出已经登记、未被拒绝或禁用且有本地媒体的表情。"""
        with closing(open_connection(self._database_file, read_only=True)) as connection:
            rows = connection.execute(
                f"""SELECT *, NULL AS source, {_REVIEW} AS review FROM emoji_assets
                   WHERE {_SENDABLE}
                   ORDER BY use_count, last_used_at, digest"""
            ).fetchall()
        unavailable = self._unavailable_digests
        return [
            entry
            for row in rows
            if row["digest"] not in unavailable and (entry := self._record(row)).has_file
        ]

    def read_media(self, digest: str) -> bytes | None:
        """读回表情字节，校验文件格式和内容地址。"""
        path = self._attachment(digest)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return None
        return data if sha256(data).hexdigest() == digest and emoji_media_type(data) else None

    def read_sendable(self, digest: str) -> bytes | None:
        """实际发送时再次核对表情使用资格，并读取完整媒体。"""
        with closing(open_connection(self._database_file)) as connection:
            allowed = connection.execute(
                f"SELECT 1 FROM emoji_assets WHERE digest = ? AND {_SENDABLE}",
                (digest,),
            ).fetchone()
        return self.read_media(digest) if allowed else None

    def read_catalog_media(self, digest: str) -> bytes | None:
        """读取目录里已有的表情原件，包含未登记或禁用的原件。"""
        with closing(open_connection(self._database_file)) as connection:
            entry = connection.execute(
                "SELECT 1 FROM emoji_assets WHERE digest = ?", (digest,)
            ).fetchone()
        return self.read_media(digest) if entry else None

    def read_catalog_description(self, digest: str) -> str | None:
        """按内容指纹读取未移除的表情描述；不推断描述作者。"""
        with closing(open_connection(self._database_file, read_only=True)) as connection:
            entry = connection.execute(
                """SELECT description FROM emoji_assets WHERE digest = ? AND NOT EXISTS (
                    SELECT 1 FROM emoji_removed WHERE entry_key = 'asset:' || digest)""",
                (digest,),
            ).fetchone()
        return (entry["description"].strip() or None) if entry is not None else None

    def _record(self, row: Row) -> EmojiRecord:
        path = self._attachment(row["digest"])
        return EmojiRecord(
            row["digest"],
            row["description"],
            tuple(json.loads(row["tags"])),
            bool(row["is_registered"]),
            bool(row["is_banned"]),
            row["collection_count"],
            row["use_count"],
            path is not None and path.is_file(),
            row["source"],
            review=row["review"],
            collected_at=row["first_collected_at"],
            last_used_at=row["last_used_at"],
        )

    def _attachment(self, digest: str | None) -> Path | None:
        if digest is None or _DIGEST.fullmatch(digest) is None:
            return None
        path = self._attachments / digest
        return (
            path
            if not path.is_symlink() and path.resolve().is_relative_to(self._memory_dir.resolve())
            else None
        )

    def _write_media(self, digest: str, data: bytes) -> None:
        self._attachments.mkdir(parents=True, exist_ok=True)
        path = self._attachment(digest)
        if path is None:
            raise OSError("表情归档目录不可用。")
        if self.read_media(digest) is not None:
            return
        with NamedTemporaryFile(dir=self._attachments, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.close()
                try:
                    path.hardlink_to(temporary)
                except FileExistsError:
                    if self.read_media(digest) is None:
                        raise OSError("表情归档文件与内容地址不符。") from None
            finally:
                temporary.unlink(missing_ok=True)
