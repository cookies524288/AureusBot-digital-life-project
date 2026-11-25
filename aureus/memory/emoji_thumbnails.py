"""MaiBot 062d45c 缩略图服务（GPL-3.0），使用宿主内容地址和数据目录。"""

import re
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from PIL import Image

_DIGEST = re.compile(r"[a-f0-9]{64}")
_SIZE = (200, 200)
_QUALITY = 80
_MAX_CACHE_BYTES = 64 * 1024 * 1024
_MAX_THUMBNAIL_BYTES = 256 * 1024
_MAX_FRAME_PIXELS = 4_000_000


class EmojiThumbnailCache:
    """只持久化目录原件的可再生 WebP；清理不接触原件。"""

    def __init__(self, root: Path, digests: Callable[[], set[str]]) -> None:
        self._root = root
        self._directory = root / "emoji_thumbnails"
        self._digests = digests
        self._lock = Lock()

    def _usable_directory(self) -> Path:
        directory = self._directory
        if (directory.is_symlink() or directory.is_junction()
                or not directory.resolve().is_relative_to(self._root.resolve())):
            raise OSError("缩略图缓存目录不可用。")
        return directory

    def _files(self) -> dict[str, tuple[Path, int]]:
        return {
            path.stem: (path, path.stat().st_size)
            for path in self._usable_directory().glob("*.webp")
            if _DIGEST.fullmatch(path.stem) and not path.is_symlink() and path.is_file()
        }

    def render(self, digest: str, source: bytes) -> tuple[bytes, bool]:
        """沿用原版首帧、模式转换、Lanczos 和 WebP 参数。"""
        if not _DIGEST.fullmatch(digest):
            raise ValueError("表情编号无效。")
        with self._lock:
            directory = self._usable_directory()
            path = directory / f"{digest}.webp"
            if not path.is_symlink() and path.is_file():
                with path.open("rb") as stream:
                    data = stream.read(_MAX_THUMBNAIL_BYTES + 1)
                if (
                    0 < len(data) <= _MAX_THUMBNAIL_BYTES
                    and data[:4] == b"RIFF"
                    and data[8:12] == b"WEBP"
                ):
                    return data, False
            with Image.open(BytesIO(source)) as image, BytesIO() as output:
                if image.width * image.height > _MAX_FRAME_PIXELS:
                    raise ValueError("表情尺寸过大，不能生成缩略图。")
                image.seek(0)
                mode = "RGBA" if image.mode in ("P", "PA", "LA", "RGBA") else "RGB"
                with image.convert(mode) as thumbnail:
                    thumbnail.thumbnail(_SIZE, Image.Resampling.LANCZOS)
                    thumbnail.save(output, "WEBP", quality=_QUALITY, method=6)
                data = output.getvalue()
            files = self._files()
            size = sum(length for _, length in files.values()) + len(data)
            for _, (old, length) in sorted(
                files.items(), key=lambda item: item[1][0].stat().st_mtime_ns
            ):
                if size <= _MAX_CACHE_BYTES:
                    break
                old.unlink(missing_ok=True)
                size -= length
            directory.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(data)
                    stream.close()
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            return data, True

    def statistics(self) -> dict[str, int | float | str]:
        """统计真实缓存文件及当前未移除资源的覆盖率。"""
        with self._lock:
            files = self._files()
            valid = self._digests()
            return {
                "cache_dir": self._directory.name,
                "total_count": len(files),
                "total_size_mb": sum(length for _, length in files.values()) / (1024 * 1024),
                "emoji_count": len(valid),
                "coverage_percent": round(100 * len(files.keys() & valid) / len(valid), 1)
                if valid
                else 0,
            }

    def cleanup(self, *, clear: bool = False) -> tuple[int, int]:
        """按目录原件保留缩略图，清空时仅删受管派生文件。"""
        with self._lock:
            files = self._files()
            valid = set() if clear else self._digests()
            removed = files.keys() - valid
            for digest in removed:
                files[digest][0].unlink(missing_ok=True)
            return len(removed), len(files) - len(removed)
