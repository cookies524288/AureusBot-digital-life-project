"""心跳的 QQ 表情识别：准备媒体、构造请求和解析标签。

模板与抽帧规则来自 MaiBot 1.3.15（GPL-3.0，来源见 THIRD_PARTY.md）；
本项目单独限制解码和分配，并用 Pillow 的无符号差值直方图计算均方误差。
"""

import re
from io import BytesIO
from typing import Literal

from PIL import Image, ImageChops, ImageSequence

from aureus.memory import MAX_EMOJI_BYTES, MAX_EMOJI_RECOGNITION_TAGS, MAX_EMOJI_TAG
from aureus.organs import CallPurpose, ModelPreference, OrganRequest

type EmojiImageType = Literal["GIF", "STATIC"]

_ANALYSIS = (
    "输入类型：{image_type}\n"
    "这是一个表情包图片。输入类型为 GIF 时，图片由动态图中抽取的不同帧横向拼接而成；"
    "输入类型为 STATIC 时，图片为单张静态图。\n"
    "请提取这个表情包主要表达的情绪、语气或常见使用场景标签，最多 5 个，使用逗号分隔。"
    "只返回纯文本标签列表，不要解释或输出其他内容。"
)
_MAX_FRAME_PIXELS = 4_000_000
_MAX_TOTAL_PIXELS = 64_000_000
_MAX_SHEET_PIXELS = 4_000_000
_MAX_FRAMES = 200
_MAX_SELECTED = 15
_TARGET_HEIGHT = 200
_SIMILARITY_THRESHOLD = 1000
_JPEG_QUALITY = 85
_STATIC_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_TAG_SEPARATOR = re.compile(r"[,，、;；\r\n\t]+")
_PLAIN_TAG = re.compile(r"[^\W_][\w -]*")


def build_emoji_analysis_request(
    image_type: EmojiImageType,
    preference: ModelPreference = ModelPreference.PREFERRED,
    *,
    data: bytes | None = None,
    mime: str | None = None,
) -> OrganRequest:
    """组装 QQ 表情识别请求；网页预览只提供输入分类。"""
    return OrganRequest(
        "vlm",
        _ANALYSIS.format(image_type=image_type),
        purpose=CallPurpose.OPTIONAL,
        model_preference=preference,
        media_bytes=data,
        media_type=mime,
    )


def prepare_emoji_image(data: bytes, mime: str) -> tuple[EmojiImageType, bytes, str]:
    """验证静态表情或生成有界 GIF 拼图，失败时抛出 ValueError。"""
    if not data or len(data) > MAX_EMOJI_BYTES:
        raise ValueError("表情原件超过识别大小限制。")
    try:
        if mime == "image/gif":
            frame_count = _check_gif(data)
            return "GIF", _make_gif_sheet(data, frame_count), "image/jpeg"
        with Image.open(BytesIO(data), formats=tuple(_STATIC_FORMATS)) as image:
            _check_static(image, mime)
            image.verify()
        with Image.open(BytesIO(data), formats=tuple(_STATIC_FORMATS)) as image:
            image.load()
        return "STATIC", data, mime
    except (OSError, SyntaxError, EOFError, Image.DecompressionBombError):
        raise ValueError("表情损坏或无法完整读取。") from None


def _check_static(image: Image.Image, mime: str) -> None:
    if (
        image.format is None
        or _STATIC_FORMATS[image.format] != mime
        or image.width * image.height > _MAX_FRAME_PIXELS
        or getattr(image, "is_animated", False)
    ):
        raise ValueError("不是支持的单张静态表情。")


def _gif_size(data: bytes, start: int) -> int:
    return int.from_bytes(data[start : start + 2], "little")


def _palette_end(start: int, packed: int) -> int:
    return start + (3 * (2 << (packed & 7)) if packed & 128 else 0)


def _skip_blocks(data: bytes, start: int) -> int:
    position = start
    while block_size := data[position]:
        position += 1 + block_size
    return position + 1


def _check_gif(data: bytes) -> int:
    """在 Pillow 处理帧和处置区之前，核对 GIF 画布、子块和帧数。"""
    if not data.startswith((b"GIF87a", b"GIF89a")) or len(data) < 13:
        raise ValueError("GIF 头部无效。")
    width, height = _gif_size(data, 6), _gif_size(data, 8)
    pixels = width * height
    if not 0 < pixels <= _MAX_FRAME_PIXELS:
        raise ValueError("GIF 画布过大或为空。")
    position = _palette_end(13, data[10])
    count = 0
    try:
        while True:
            marker = data[position]
            if marker == 0x3B and count:
                return count
            if marker == 0x21:
                position = _skip_blocks(data, position + 2)
                continue
            if marker != 0x2C:
                break
            left = _gif_size(data, position + 1)
            top = _gif_size(data, position + 3)
            frame_width = _gif_size(data, position + 5)
            frame_height = _gif_size(data, position + 7)
            if not (0 < frame_width <= width - left and 0 < frame_height <= height - top):
                raise ValueError("GIF 帧越出原始画布。")
            count += 1
            if count > _MAX_FRAMES or count * pixels > _MAX_TOTAL_PIXELS:
                raise ValueError("GIF 帧数或总像素过大。")
            position = _palette_end(position + 10, data[position + 9])
            position = _skip_blocks(data, position + 1)
    except IndexError:
        raise ValueError("GIF 数据不完整。") from None
    raise ValueError("GIF 数据不完整。")


def _frame_difference(previous: Image.Image, frame: Image.Image) -> float:
    histogram = ImageChops.difference(previous, frame).histogram()
    squared_distance = sum((index % 256) ** 2 * count for index, count in enumerate(histogram))
    return squared_distance / (frame.width * frame.height * len(frame.getbands()))


def _make_gif_sheet(data: bytes, frame_count: int) -> bytes:
    selected: list[Image.Image] = []
    previous: Image.Image | None = None
    with Image.open(BytesIO(data), formats=("GIF",)) as animation:
        width = max(1, animation.width * _TARGET_HEIGHT // animation.height)
        decoded = 0
        for frame in ImageSequence.Iterator(animation):
            decoded += 1
            frame.load()
            if len(selected) == _MAX_SELECTED:
                continue
            rgb = frame.convert("RGB")
            if previous is not None and _frame_difference(previous, rgb) <= _SIMILARITY_THRESHOLD:
                continue
            total_width = width * (len(selected) + 1)
            if total_width * _TARGET_HEIGHT > _MAX_SHEET_PIXELS:
                raise ValueError("GIF 表情拼图过大。")
            selected.append(rgb.resize((width, _TARGET_HEIGHT), Image.Resampling.LANCZOS))
            previous = rgb
    if decoded != frame_count or not selected:
        raise ValueError("GIF 解码帧数异常。")
    with Image.new("RGB", (width * len(selected), _TARGET_HEIGHT)) as sheet:
        for index, frame in enumerate(selected):
            sheet.paste(frame, (index * width, 0))
        with BytesIO() as buffer:
            sheet.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
            return buffer.getvalue()


def parse_emoji_tags(suggestion: str) -> tuple[str, ...]:
    """仅接受短纯文本标签，按首次出现顺序保留至多五项。"""
    tags: dict[str, str] = {}
    for part in _TAG_SEPARATOR.split(suggestion):
        tag = part.strip()
        if not tag:
            continue
        if len(tag) > MAX_EMOJI_TAG or _PLAIN_TAG.fullmatch(tag) is None:
            return ()
        tags.setdefault(tag.casefold(), tag)
    return tuple(tags.values())[:MAX_EMOJI_RECOGNITION_TAGS]
