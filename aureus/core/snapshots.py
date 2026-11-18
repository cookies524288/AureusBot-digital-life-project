"""快照的大小上限：总大小超了就从最旧的开始删（D46）。

快照是导入前的备份和以后的定期快照，会一直积累；不设上限会悄悄占满用户指定的盘。
但它们是用户的后路，所以最新的一份永远保留。
"""

from pathlib import Path

from loguru import logger

BYTES_PER_MEGABYTE = 1024 * 1024
SNAPSHOT_SUFFIX = ".zip"


def _list_snapshots(snapshots_dir: Path) -> list[tuple[Path, float, int]]:
    """列出快照（路径、修改时间、大小），从旧到新。读不到信息的文件跳过并写警告。

    文件可能恰好被别的程序删掉或没有权限；这种文件不算进总大小，也不会被删，
    清理不能因为它们让启动失败。
    """
    found: list[tuple[Path, float, int]] = []
    for item in snapshots_dir.iterdir():
        try:
            if not item.is_file() or item.suffix != SNAPSHOT_SUFFIX:
                continue
            info = item.stat()
        except OSError as error:
            logger.warning("快照 {} 读不到信息（{}），跳过", item.name, type(error).__name__)
            continue
        found.append((item, info.st_mtime, info.st_size))
    found.sort(key=lambda entry: (entry[1], entry[0].name))
    return found


def prune_snapshots(snapshots_dir: Path, max_total_bytes: int) -> list[Path]:
    """删掉最旧的快照，直到总大小不超过上限。

    只看这个文件夹里的 .zip 普通文件，不进子文件夹，别的文件不碰。最新的一份永远保留，
    哪怕它自己就超过上限。删不掉的文件（被占用、没权限）跳过并写警告，不中断启动。

    Args:
        snapshots_dir: 快照文件夹；不存在时什么都不做。
        max_total_bytes: 允许的总字节数。

    Returns:
        被删掉的文件，从旧到新。
    """
    if not snapshots_dir.is_dir():
        return []
    found = _list_snapshots(snapshots_dir)
    sizes = {item: size for item, _modified, size in found}
    snapshots = [item for item, _modified, _size in found]
    total = sum(sizes.values())
    removed: list[Path] = []
    for snapshot in snapshots[:-1]:
        if total <= max_total_bytes:
            break
        size = sizes[snapshot]
        try:
            snapshot.unlink()
        except OSError as error:
            logger.warning("快照 {} 删不掉（{}），跳过", snapshot.name, type(error).__name__)
            continue
        total -= size
        removed.append(snapshot)
        logger.info("快照总大小超过上限，已删除最旧的 {}（{} 字节）", snapshot.name, size)
    return removed
