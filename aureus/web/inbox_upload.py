"""限定桌面可上传的浏览器选件，不接受客户端路径、URL 或归档命令。"""

import asyncio
import re
from io import BytesIO

from fastapi import HTTPException, Request
from PIL import Image

# 使用一个有界 OneBot 帧，包含 Base64 后仍小于 1 MiB。
MAX_FILE_BYTES = 700 * 1024
MAX_IMAGE_PIXELS = 16_000_000
_NAME = re.compile(r"[^\W.][\w .()-]{0,95}\.(?:txt|png|jpe?g|gif|webp)", re.IGNORECASE)
_TYPES = {
    "txt": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


async def read_upload(request: Request, name: str) -> bytes:
    """流式限制请求大小，再确认简单文件名、类型和文件内容彼此一致。"""
    if not _NAME.fullmatch(name):
        raise HTTPException(422, "文件名称或后缀不支持；不接受路径或网址。")
    expected = _TYPES[name.rsplit(".", 1)[1].lower()]
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != expected:
        raise HTTPException(415, "文件类型与文件名不匹配。")
    content = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_FILE_BYTES - len(content):
            raise HTTPException(413, "文件不能超过 700 KiB。")
        content.extend(chunk)
    data = bytes(content)
    if not data or not await asyncio.to_thread(_matches, data, expected):
        raise HTTPException(415, "文件内容不符合所选类型。")
    return data


def _matches(data: bytes, expected: str) -> bool:
    try:
        if expected == "text/plain":
            text = data.decode("utf-8-sig")
            return bool(text) and all(char.isprintable() or char in "\t\r\n" for char in text)
        with Image.open(BytesIO(data)) as image:
            if (
                image.get_format_mimetype() != expected
                or image.width * image.height > MAX_IMAGE_PIXELS
            ):
                return False
            image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False
    return True
