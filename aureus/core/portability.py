"""导出与导入（D45，14 号文档第 4 节）：把数据根目录里值得带走的东西打成一个 zip。

不含密钥（secrets.vault、secrets.key、.env，D36）、单实例锁文件和快照；日志默认不含。
数据库用 SQLite 自带的在线备份接口取一致的副本，不直接复制可能正在写的文件。
"""

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from aureus import __version__
from aureus.common import AureusPaths, Clock
from aureus.identity import IdentityService, SingleInstanceLockError

# 导出包的格式版本。以后改了包的结构就加一；导入时遇到比它新的包要拒绝（D45 第 3 条）。
EXPORT_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"


class PortabilityError(Exception):
    """导出或导入失败。消息是中文，说明原因。"""


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果。"""

    archive: Path
    file_count: int
    total_bytes: int


def _database_copy(database_file: Path) -> bytes:
    """用在线备份接口取数据库的一致副本。只读打开，不会改动原文件。

    副本先放在内存里再取出来，不往别处写临时文件（D38：所有写入都在数据根目录之内）。
    """
    uri = f"{database_file.as_uri()}?mode=ro"
    with (
        closing(sqlite3.connect(uri, uri=True)) as source,
        closing(sqlite3.connect(":memory:")) as copy,
    ):
        source.backup(copy)
        return copy.serialize()


def _files_under(folder: Path) -> list[Path]:
    """文件夹里所有普通文件，按名字排序；文件夹不存在时返回空。"""
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.rglob("*") if path.is_file())


def exportable_paths(paths: AureusPaths, *, include_logs: bool) -> list[Path]:
    """数据根目录里所有可以导出的文件（只列出，不读内容），按位置排序。

    符号链接和指到数据根目录之外的文件一律跳过：不能因为数据根目录里有个链接，
    就把别处的东西悄悄带进包里。导入时也用它判断目标里有没有已有的数据。
    """
    home = paths.home.resolve()
    candidates = [paths.runtime_config_file, paths.identity_file, paths.models_config_file]
    folders = [paths.diary_dir, paths.memory_dir, paths.skills_dir]
    if include_logs:
        folders.append(paths.log_dir)
    for folder in folders:
        candidates.extend(_files_under(folder))
    candidates.append(paths.database_file)
    return [
        path
        for path in candidates
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(home)
    ]


def collect_export_files(paths: AureusPaths, *, include_logs: bool) -> dict[str, bytes]:
    """收集要导出的文件：包内的相对路径（用 /）到内容。

    Args:
        paths: 数据根目录的路径集合。
        include_logs: 是否带上日志。

    Returns:
        包内路径到内容。数据库是一致的副本。

    Raises:
        OSError: 读文件失败。
        sqlite3.Error: 备份数据库失败（比如文件不是数据库）。
    """
    files: dict[str, bytes] = {}
    for path in exportable_paths(paths, include_logs=include_logs):
        name = path.relative_to(paths.home).as_posix()
        files[name] = _database_copy(path) if path == paths.database_file else path.read_bytes()
    return files


def build_manifest(
    files: dict[str, bytes], clock: Clock, *, include_logs: bool
) -> dict[str, object]:
    """生成 manifest：格式版本、软件版本、导出时间，以及每个文件的大小和 sha256。"""
    return {
        "export_format_version": EXPORT_FORMAT_VERSION,
        "software_version": __version__,
        "exported_at": clock.now().isoformat(),
        "includes_logs": include_logs,
        "files": [
            {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        ],
    }


def export_data(
    paths: AureusPaths, archive: Path, clock: Clock, *, include_logs: bool = False
) -> ExportResult:
    """把数据导出成 zip。

    先写成 .part 临时文件，写完再改名：中途出错不会留下一个半截的、看着像好的包。

    Args:
        paths: 数据根目录的路径集合。
        archive: 要写的 zip 文件。已经存在时拒绝，不覆盖。
        clock: 时钟，导出时间从它取。
        include_logs: 是否带上日志。

    Returns:
        导出结果。

    Raises:
        PortabilityError: 目标文件已存在，或读写数据失败。
    """
    if archive.exists():
        raise PortabilityError(f"{archive} 已经存在，不覆盖；请换一个文件名。")
    try:
        files = collect_export_files(paths, include_logs=include_logs)
    except (OSError, sqlite3.Error) as error:
        raise PortabilityError(f"读取数据时出错（{type(error).__name__}）：{error}") from error
    manifest = build_manifest(files, clock, include_logs=include_logs)
    partial = archive.with_name(archive.name + ".part")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            for name, data in files.items():
                bundle.writestr(name, data)
        partial.replace(archive)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise PortabilityError(f"写导出包时出错（{type(error).__name__}）：{error}") from error
    return ExportResult(archive, len(files), sum(len(data) for data in files.values()))


@dataclass(frozen=True)
class ImportResult:
    """一次导入的结果。"""

    file_count: int
    backup: Path | None  # 覆盖前备份的位置；没覆盖任何东西时是 None


# 导入时只允许写到这几个文件和文件夹（D45 第 4 条）。密钥文件不在其中，包里带了也不认。
_ALLOWED_FILES = frozenset(
    {"config/aureus.toml", "config/identity.toml", "config/models.toml", "data/aureus.db"}
)
_ALLOWED_FOLDERS = ("data/diary/", "data/memory/", "data/skills/", "data/logs/")


def _check_member_name(name: str) -> None:
    """包里的路径必须是允许的位置，而且不能绕出数据根目录（zip slip）。"""
    unsafe = (
        not name
        or "\\" in name
        or ":" in name
        or name.startswith("/")
        or any(part in ("", ".", "..") for part in name.split("/"))
    )
    if unsafe or not (name in _ALLOWED_FILES or name.startswith(_ALLOWED_FOLDERS)):
        raise PortabilityError(f"导出包里有不允许的路径 {name!r}，拒绝导入。")


def _manifest_entries(bundle: zipfile.ZipFile) -> list[tuple[str, int, str]]:
    """读 manifest，先查格式版本，再返回 (路径, 大小, sha256) 列表。"""
    try:
        manifest = json.loads(bundle.read(MANIFEST_NAME))
        version = manifest["export_format_version"]
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile) as error:
        raise PortabilityError(
            f"导出包里的 {MANIFEST_NAME} 缺失或格式不对（{type(error).__name__}）。"
        ) from error
    if not isinstance(version, int) or isinstance(version, bool):
        raise PortabilityError("manifest 里的 export_format_version 不是整数。")
    if version > EXPORT_FORMAT_VERSION:
        raise PortabilityError(
            f"导出包的格式版本是 {version}，比当前软件认识的 {EXPORT_FORMAT_VERSION} 新。"
            "请先升级软件。"
        )
    try:
        entries = [(item["path"], item["size"], item["sha256"]) for item in manifest["files"]]
    except (KeyError, TypeError) as error:
        raise PortabilityError("manifest 里的文件列表格式不对。") from error
    for name, size, digest in entries:
        if not (isinstance(name, str) and isinstance(size, int) and isinstance(digest, str)):
            raise PortabilityError("manifest 里有格式不对的文件条目。")
        _check_member_name(name)
    if len({name for name, _, _ in entries}) != len(entries):
        raise PortabilityError("manifest 里有重复的路径。")
    return entries


def _read_verified_files(archive: Path) -> dict[str, bytes]:
    """读出导出包里的文件，版本、路径、大小和 sha256 都核对通过才返回。"""
    try:
        bundle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as error:
        raise PortabilityError(f"打不开导出包 {archive}（{type(error).__name__}）。") from error
    files: dict[str, bytes] = {}
    with bundle:
        for name, size, digest in _manifest_entries(bundle):
            try:
                if bundle.getinfo(name).file_size != size:
                    raise PortabilityError(f"{name} 的大小和 manifest 不符，包可能损坏。")
                data = bundle.read(name)
            except (KeyError, zipfile.BadZipFile, OSError) as error:
                raise PortabilityError(
                    f"导出包里读不出 {name}（{type(error).__name__}）。"
                ) from error
            if hashlib.sha256(data).hexdigest() != digest:
                raise PortabilityError(f"{name} 的校验值和 manifest 不符，包可能损坏或被改过。")
            files[name] = data
    return files


def _install(
    paths: AureusPaths, files: dict[str, bytes], clock: Clock, *, force: bool, backup_first: bool
) -> ImportResult:
    """把核对过的文件放到位。要在拿到单实例锁之后调用。"""
    backup: Path | None = None
    has_existing_data = bool(exportable_paths(paths, include_logs=True))
    if has_existing_data and not force:
        raise PortabilityError(
            "目标数据根目录里已经有数据，不覆盖。确定要覆盖时加 --force，"
            "覆盖前会先把现有数据备份到 data/snapshots/。"
        )
    if has_existing_data and backup_first:
        stamp = clock.now().strftime("%Y%m%d-%H%M%S")
        backup = paths.snapshots_dir / f"before-import-{stamp}.zip"
        paths.snapshots_dir.mkdir(parents=True, exist_ok=True)
        # 备份失败就停下：备份不成，宁可不覆盖。
        export_data(paths, backup, clock, include_logs=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    # 先全部写进数据根目录下的临时文件夹，都写成功了再移动到位，中途出错不留下半个导入。
    staging = Path(tempfile.mkdtemp(prefix=".import-", dir=paths.data_dir))
    try:
        for name, data in files.items():
            staged = staging / name
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
        for name in files:
            destination = paths.home / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            (staging / name).replace(destination)
    except OSError as error:
        raise PortabilityError(f"放置文件时出错（{type(error).__name__}）：{error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return ImportResult(len(files), backup)


def import_data(
    paths: AureusPaths,
    archive: Path,
    clock: Clock,
    *,
    force: bool = False,
    backup_first: bool = True,
) -> ImportResult:
    """把导出包导入数据根目录。永远不碰密钥文件，也不删除包里没有的文件。

    Args:
        paths: 目标数据根目录的路径集合。
        archive: 导出包。
        clock: 时钟，备份文件名里的时间从它取。
        force: 目标已有数据时是否覆盖。
        backup_first: 覆盖前是否先备份旧数据；默认是。只在 force 时有意义，
            关掉后被覆盖的旧数据无法找回。

    Returns:
        导入结果。

    Raises:
        PortabilityError: 包打不开、被改过、格式太新、路径不安全，有实例在运行，
            或目标已有数据又没有 force。
    """
    files = _read_verified_files(archive)
    try:
        with IdentityService(paths).acquire_lock():
            return _install(paths, files, clock, force=force, backup_first=backup_first)
    except SingleInstanceLockError as error:
        raise PortabilityError(str(error)) from error
