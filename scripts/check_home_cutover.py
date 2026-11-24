"""维护工具：Windows 停机整目录复制及校验，不启动 Aureus 或改启动路径。"""

import argparse
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

from filelock import FileLock, Timeout

from aureus.common import AureusPaths

LOCK_NAME = "aureus.lock"
DEFAULT_HOME_NAME = "aureus_home"


class CutoverError(RuntimeError):
    """目录不符合离线复制前提，或复制结果未获验证。"""


def _checked_stat(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise CutoverError("路径包含重解析点或符号链接，不能复制。")
    return info


def _checked_root(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or path.drive.startswith("\\\\"):
        raise CutoverError("必须使用本地现有目录的绝对路径，不能包含 '..'。")
    for part in (*reversed(path.parents), path):
        if not stat.S_ISDIR(_checked_stat(part).st_mode):
            raise CutoverError("两端都必须是现有目录。")
    return path.resolve(strict=True)


def _layout(root: Path, *, skip_lock: bool) -> dict[Path, bool]:
    entries: dict[Path, bool] = {}
    pending = [root]
    while pending:
        for path in pending.pop().iterdir():
            relative = path.relative_to(root)
            mode = _checked_stat(path).st_mode
            if skip_lock and relative == Path(LOCK_NAME):
                if not stat.S_ISREG(mode):
                    raise CutoverError("源运行锁不是普通文件。")
                continue
            if stat.S_ISDIR(mode):
                entries[relative] = True
                pending.append(path)
            elif stat.S_ISREG(mode):
                entries[relative] = False
            else:
                raise CutoverError("发现非普通文件或目录，不能复制。")
    return entries


def _digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def _verify_copy(source: Path, target: Path, layout: dict[Path, bool]) -> int:
    if _layout(source, skip_lock=True) != layout or _layout(target, skip_lock=False) != layout:
        raise CutoverError("复制前后目录结构不一致；保留两端，不切换。")
    files = [relative for relative, is_directory in layout.items() if not is_directory]
    if any(_digest(source / relative) != _digest(target / relative) for relative in files):
        raise CutoverError("文件内容校验不一致；保留两端，不切换。")
    return len(files)


def copy_checked_home(source: Path, target: Path, *, confirmed_stopped: bool) -> int:
    """停机后复制完整普通文件树，核对通过才返回文件数；否则抛出错误并保留两端。"""
    if sys.platform != "win32" or not confirmed_stopped:
        raise CutoverError("只在 Windows 确认正常停机后使用；须明确传入 --confirm-stopped。")
    source = _checked_root(source)
    target = _checked_root(target)
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise CutoverError("源、目标不能相同或互相包含。")
    if target.name != DEFAULT_HOME_NAME or next(target.iterdir(), None) is not None:
        raise CutoverError("目标必须是已有且完全为空的 aureus_home 目录。")
    layout = _layout(source, skip_lock=True)
    database = AureusPaths(source).database_file.relative_to(source)
    if layout.get(database) is not False:
        raise CutoverError("源缺少 Aureus 核心数据库。")
    lock_path = source / LOCK_NAME
    try:
        with FileLock(lock_path, timeout=0, preserve_lock_file=True, fallback_to_soft=False):
            if next(target.iterdir(), None) is not None:
                raise CutoverError("目标已不为空；保留两端，不复制。")
            result = subprocess.run(
                [
                    "robocopy.exe",
                    str(source),
                    str(target),
                    "/E",
                    "/COPY:DAT",
                    "/DCOPY:DAT",
                    "/R:0",
                    "/W:0",
                    "/XJ",
                    "/SL",
                    "/XC",
                    "/XN",
                    "/XO",
                    "/XF",
                    str(lock_path),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode not in {0, 1}:
                raise CutoverError(
                    f"Robocopy 结果异常（退出码 {result.returncode}）；保留两端，不切换。"
                )
            return _verify_copy(source, target, layout)
    except Timeout as error:
        raise CutoverError("源单实例锁未取得；可能仍在运行，不复制。") from error


def main() -> int:
    """解析显式目录与停机确认参数，只报告校验状态与文件数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--confirm-stopped", action="store_true")
    args = parser.parse_args()
    try:
        count = copy_checked_home(args.source, args.target, confirmed_stopped=args.confirm_stopped)
    except CutoverError as error:
        print(f"未通过：{error}", file=sys.stderr)
        return 1
    except OSError:
        print("未通过：无法访问目录、文件或 Robocopy；保留两端，不切换。", file=sys.stderr)
        return 1
    print(f"复制并校验通过：{count} 个文件；启动路径未修改。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
