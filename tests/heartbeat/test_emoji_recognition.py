"""验证 QQ 表情 GIF 抽帧、解码限制和标签列表格式。"""

from io import BytesIO

import pytest
from PIL import Image

from aureus.heartbeat.emoji_recognition import parse_emoji_tags, prepare_emoji_image


def _animation(size: tuple[int, int], colors: list[tuple[int, int, int]]) -> bytes:
    frames = [Image.new("RGB", size, color) for color in colors]
    with BytesIO() as buffer:
        frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:])
        return buffer.getvalue()


def test_gif_sheet_skips_similar_frames_and_keeps_fifteen() -> None:
    colors = [(255, 0, 0), (254, 0, 0), *([(0, 0, 255), (255, 0, 0)] * 10)]
    kind, encoded, mime = prepare_emoji_image(_animation((4, 4), colors), "image/gif")
    assert (kind, mime) == ("GIF", "image/jpeg")
    with Image.open(BytesIO(encoded)) as sheet:
        assert sheet.size == (15 * 200, 200)
        assert sheet.getpixel((100, 100)) == (254, 0, 0)
        assert sheet.getpixel((300, 100)) == (0, 0, 254)
        assert sheet.getpixel((500, 100)) == (254, 0, 0)


@pytest.mark.parametrize(
    "limit,error",
    [
        ("frame_count", "帧数或总像素"),
        ("total_pixels", "帧数或总像素"),
        ("canvas", "画布"),
        ("frame_extent", "越出"),
        ("composition", "拼图过大"),
        ("trailer", "不完整"),
    ],
)
def test_gif_limits_reject_before_sending(limit: str, error: str) -> None:
    colors = [(255, 0, 0), (0, 0, 255)]
    if limit == "frame_count":
        data = _animation((4, 4), colors * 101)
    elif limit == "total_pixels":
        simple = _animation((4, 4), colors * 9)
        data = simple[:6] + (2000).to_bytes(2, "little") * 2 + simple[10:]
    elif limit == "composition":
        data = _animation((101, 1), colors)
    else:
        simple = _animation((4, 4), colors)
        if limit == "canvas":
            data = simple[:6] + (2001).to_bytes(2, "little") * 2 + simple[10:]
        elif limit == "frame_extent":
            frame_start = 13 + 3 * (2 << (simple[10] & 7))
            assert simple[frame_start] == 0x2C
            data = simple[: frame_start + 5] + (5).to_bytes(2, "little") + simple[frame_start + 7 :]
        else:
            data = _animation((4, 4), colors * 8)[:-1]
    with pytest.raises(ValueError, match=error):
        prepare_emoji_image(data, "image/gif")


@pytest.mark.parametrize(
    "suggestion", ["", "，,;", "```惊讶```", "标签：惊讶", "a" * 41, '["惊讶"]']
)
def test_invalid_emoji_labels_are_not_invented(suggestion: str) -> None:
    assert parse_emoji_tags(suggestion) == ()


def test_emoji_labels_accept_distinct_plain_tags_up_to_five() -> None:
    assert parse_emoji_tags(" 满意，Hi, hi\t庆祝、；迟疑; 打招呼\n第六项") == (
        "满意",
        "Hi",
        "庆祝",
        "迟疑",
        "打招呼",
    )
