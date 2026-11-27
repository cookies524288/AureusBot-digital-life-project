"""仅解读本会话已经取得的附件字节；没有解码器或读不到就不生成说明。"""

import re
import shutil
import subprocess
from pathlib import Path

from aureus.common.chat_documents import packaged_document_text

_MEDIA_LIMIT = 8 * 1024 * 1024
_DOCUMENT_LIMIT = 2000
_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".srt", ".vtt"}
_TIMESTAMP = re.compile(r"(?<!\d)(?:(\d{1,3})[:：](\d{2})|(\d{1,4})\s*秒)")


def document_excerpt(data: bytes, name: str, question: str) -> str:
    """从已核对的文本、Office 或 PDF 原件中取问句附近片段；未命中则取开头。"""
    if not data or len(data) > _MEDIA_LIMIT:
        return ""
    if Path(name).suffix.lower() in _TEXT_SUFFIXES:
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        try:
            text = data.decode(encoding).strip()
        except UnicodeError:
            return ""
    else:
        text = packaged_document_text(data, name)
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        return ""
    if len(text) <= _DOCUMENT_LIMIT:
        return text
    words = re.findall(r"[\w]{2,80}", question)
    offsets = [text.find(word) for word in words if word in text]
    start = max(0, min(offsets, default=0) - _DOCUMENT_LIMIT // 4)
    return text[start : start + _DOCUMENT_LIMIT]


def requested_second(question: str) -> int | None:
    """解析问句中显式给出的分:秒或秒数；没有位置就从开头看。"""
    match = _TIMESTAMP.search(question)
    if match is None:
        return None
    if match[3] is not None:
        return int(match[3])
    return int(match[1]) * 60 + int(match[2])


def sample_clip(data: bytes, mime: str, start: int, *, audio: bool) -> bytes | None:
    """可选本地 FFmpeg，仅管道输入输出，限格式、线程、片长与解码时长。"""
    formats = {
        "video/mp4": "mov",
        "video/webm": "matroska",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
    }
    decoder = shutil.which("ffmpeg")
    if not decoder or not data or len(data) > _MEDIA_LIMIT or mime not in formats:
        return None
    output = (
        [
            "-vn",
            "-t",
            "12",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-id3v2_version",
            "0",
            "-write_xing",
            "0",
            "-f",
            "mp3",
        ]
        if audio
        else [
            "-an",
            "-frames:v",
            "1",
            "-vf",
            "scale=384:216:force_original_aspect_ratio=decrease,pad=384:216:(ow-iw)/2:(oh-ih)/2",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
        ]
    )
    try:
        result = subprocess.run(
            [
                decoder,
                "-nostdin",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-max_alloc",
                "33554432",
                "-threads",
                "1",
                "-f",
                formats[mime],
                "-i",
                "pipe:0",
                "-ss",
                str(start),
                "-threads",
                "1",
                *output,
                "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout or None
