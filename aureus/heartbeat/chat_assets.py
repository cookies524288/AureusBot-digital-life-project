"""本地维护者给具体 QQ 会话单独放行的素材；模型只能选择已存在的文件名。"""

import re
from io import BytesIO
from pathlib import Path

from PIL import Image

from .inbox_store import ConversationKey

_MAX_BYTES = 700 * 1024
_NAME = re.compile(r"[^\W.][\w .()-]{0,95}\.(?:txt|png|jpe?g|gif|webp)", re.IGNORECASE)
_TYPES = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP"}


class ApprovedAssets:
    """仅普通群/私聊可读 root/机器人账号/会话类型/会话号/文件名，不跟随链接。"""

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def _scope(self, key: ConversationKey) -> Path | None:
        if (
            key.platform != "qq"
            or key.kind not in ("group", "private")
            or key.temporary_group_id
            or not re.fullmatch(r"[0-9]{1,20}", key.account_id)
            or not re.fullmatch(r"[0-9]{1,20}", key.recipient_id)
        ):
            return None
        directory = self.root / key.account_id / key.kind / key.recipient_id
        return directory if directory.resolve() == directory and directory.is_dir() else None

    def names(self, key: ConversationKey) -> tuple[str, ...]:
        """只列当前目的地的白名单文件名，不公开目录。"""
        try:
            directory = self._scope(key)
            if directory is None:
                return ()
            return tuple(
                sorted(item.name for item in directory.iterdir() if self.allows(key, item.name))[
                    :20
                ]
            )
        except OSError:
            return ()

    def allows(self, key: ConversationKey, name: str) -> bool:
        """实际发出前可再次核对维护者是否移除了对应授权。"""
        if not _NAME.fullmatch(name):
            return False
        try:
            directory = self._scope(key)
            if directory is None:
                return False
            item = directory / name
            return (
                item.resolve() == item and item.is_file() and 0 < item.stat().st_size <= _MAX_BYTES
            )
        except OSError:
            return False

    def read(self, key: ConversationKey, name: str) -> tuple[str, bytes] | None:
        """读出有界原件，拒绝与图片后缀不符的数据或伪装成文本的二进制。"""
        if not self.allows(key, name):
            return None
        item = self.root / key.account_id / key.kind / key.recipient_id / name
        try:
            with item.open("rb") as source:
                data = source.read(_MAX_BYTES + 1)
            if not data or len(data) > _MAX_BYTES or not self.allows(key, name):
                return None
            if item.suffix.lower() == ".txt":
                if not all(
                    char.isprintable() or char in "\t\r\n" for char in data.decode("utf-8-sig")
                ):
                    return None
            else:
                with Image.open(BytesIO(data), formats=(_TYPES[item.suffix.lower()],)) as image:
                    if image.width * image.height > 16_000_000:
                        return None
                    image.verify()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            return None
        return name, data
