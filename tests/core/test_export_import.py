"""导出与导入的测试（D45）：临时数据根目录，全部用编造的假密钥。"""

import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import AureusPaths, ManualClock
from aureus.common.vault import SecretVault
from aureus.core import portability
from aureus.core.portability import (
    EXPORT_FORMAT_VERSION,
    MANIFEST_NAME,
    PortabilityError,
    export_data,
    import_data,
)
from aureus.identity import IdentityService
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery

CLOCK = ManualClock(datetime(2026, 11, 17, 8, 0, tzinfo=UTC))
SECRET = "sk-fake-export-0001"
KEY_NAME = "AUREUS_KEY_FAKE"
RECORD = "导出前的一条记录"


def populate(paths: AureusPaths, record: str = RECORD) -> None:
    """让数据根目录里有配置、密钥、日记、日志、快照和一条时间线记录。"""
    paths.ensure_directories()
    paths.identity_file.write_text("[facts]\nname = '甲'\n", encoding="utf-8")
    paths.runtime_config_file.write_text("log_level = 'INFO'\n", encoding="utf-8")
    SecretVault(paths.vault_file, paths.master_key_file).set(KEY_NAME, SECRET)
    paths.env_file.write_text(f"{KEY_NAME}={SECRET}\n", encoding="utf-8")
    (paths.diary_dir / "2026-11-17.md").write_text("今天的日记", encoding="utf-8")
    (paths.log_dir / "aureus.log").write_text("一行日志", encoding="utf-8")
    (paths.snapshots_dir / "old.zip").write_bytes(b"snapshot")
    database = TimelineDatabase(paths.database_file)
    database.open()
    try:
        database.append("test", record, ActorType.ME)
    finally:
        database.close()


def names(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as bundle:
        return set(bundle.namelist())


def records(paths: AureusPaths) -> list[str]:
    database = TimelineDatabase(paths.database_file)
    database.open()
    try:
        return [item.content for item in database.query(TimelineQuery())]
    finally:
        database.close()


def craft(
    archive: Path,
    files: dict[str, bytes],
    *,
    version: int = EXPORT_FORMAT_VERSION,
    bad_digest: bool = False,
) -> None:
    """手工做一个导出包，用来测试被改过、路径不安全或版本太新的包。"""
    entries = [
        {
            "path": name,
            "size": len(data),
            "sha256": "0" * 64 if bad_digest else hashlib.sha256(data).hexdigest(),
        }
        for name, data in files.items()
    ]
    manifest = {"export_format_version": version, "files": entries}
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(MANIFEST_NAME, json.dumps(manifest))
        for name, data in files.items():
            bundle.writestr(name, data)


@pytest.fixture
def source(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "source")
    populate(paths)
    return paths


@pytest.fixture
def target(tmp_path: Path) -> AureusPaths:
    return AureusPaths(tmp_path / "target")


def test_export_has_data_but_never_secrets(source: AureusPaths, tmp_path: Path) -> None:
    archive = tmp_path / "out.zip"
    result = export_data(source, archive, CLOCK)
    assert names(archive) == {
        MANIFEST_NAME,
        "config/identity.toml",
        "config/aureus.toml",
        "data/aureus.db",
        "data/diary/2026-11-17.md",
    }
    assert result.file_count == 4
    with zipfile.ZipFile(archive) as bundle:
        assert all(SECRET.encode() not in bundle.read(name) for name in bundle.namelist())


def test_export_includes_logs_only_when_asked(source: AureusPaths, tmp_path: Path) -> None:
    export_data(source, tmp_path / "with.zip", CLOCK, include_logs=True)
    assert "data/logs/aureus.log" in names(tmp_path / "with.zip")
    export_data(source, tmp_path / "without.zip", CLOCK)
    assert "data/logs/aureus.log" not in names(tmp_path / "without.zip")


def test_export_excludes_lock_markers(source: AureusPaths, tmp_path: Path) -> None:
    (source.home / "aureus.lock").write_text("", encoding="utf-8")
    (source.home / "aureus.lock.pid").write_text("999999", encoding="utf-8")

    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)

    exported = names(archive)
    assert "aureus.lock" not in exported
    assert "aureus.lock.pid" not in exported


def test_export_leaves_the_source_untouched(source: AureusPaths, tmp_path: Path) -> None:
    export_data(source, tmp_path / "out.zip", CLOCK)
    assert records(source) == [RECORD]
    assert not list(tmp_path.glob("*.part"))


def test_export_refuses_to_overwrite_an_existing_file(source: AureusPaths, tmp_path: Path) -> None:
    archive = tmp_path / "out.zip"
    archive.write_bytes(b"keep me")
    with pytest.raises(PortabilityError, match="已经存在"):
        export_data(source, archive, CLOCK)
    assert archive.read_bytes() == b"keep me"


def test_round_trip_restores_data_and_creates_no_secrets(
    source: AureusPaths, target: AureusPaths, tmp_path: Path
) -> None:
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    result = import_data(target, archive, CLOCK)
    assert result.file_count == 4
    assert result.backup is None
    assert records(target) == [RECORD]
    assert target.identity_file.read_text(encoding="utf-8") == "[facts]\nname = '甲'\n"
    assert not target.vault_file.exists()
    assert not target.master_key_file.exists()
    assert not target.env_file.exists()
    assert not list(target.data_dir.glob(".import-*"))


def test_import_refuses_to_overwrite_existing_data(
    source: AureusPaths, target: AureusPaths, tmp_path: Path
) -> None:
    populate(target, "目标原有的记录")
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    with pytest.raises(PortabilityError, match="--force"):
        import_data(target, archive, CLOCK)
    assert records(target) == ["目标原有的记录"]


def test_force_backs_up_first_and_never_touches_secrets(
    source: AureusPaths, target: AureusPaths, tmp_path: Path
) -> None:
    populate(target, "目标原有的记录")
    vault_before = target.vault_file.read_bytes()
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    result = import_data(target, archive, CLOCK, force=True)
    assert records(target) == [RECORD]
    assert result.backup is not None
    assert result.backup.parent == target.snapshots_dir
    assert "config/identity.toml" in names(result.backup)
    assert target.vault_file.read_bytes() == vault_before


def test_import_is_refused_while_an_instance_is_running(
    source: AureusPaths, target: AureusPaths, tmp_path: Path
) -> None:
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    with IdentityService(target).acquire_lock(), pytest.raises(PortabilityError, match="另一个"):
        import_data(target, archive, CLOCK)
    assert not target.database_file.exists()


def test_tampered_package_is_refused_and_writes_nothing(
    target: AureusPaths, tmp_path: Path
) -> None:
    archive = tmp_path / "bad.zip"
    craft(archive, {"config/identity.toml": b"x"}, bad_digest=True)
    with pytest.raises(PortabilityError, match="校验"):
        import_data(target, archive, CLOCK)
    assert not target.home.exists()


@pytest.mark.parametrize(
    "name",
    [
        "../evil.txt",
        "/abs.txt",
        "C:/x.txt",
        "secrets.key",
        ".env",
        "data/../../x",
        "data/diary/../../../x",
    ],
)
def test_unsafe_or_unlisted_paths_are_refused(
    name: str, target: AureusPaths, tmp_path: Path
) -> None:
    archive = tmp_path / "bad.zip"
    craft(archive, {name: b"x"})
    with pytest.raises(PortabilityError, match="不允许的路径"):
        import_data(target, archive, CLOCK)
    assert not target.home.exists()
    assert not (tmp_path / "evil.txt").exists()


def test_newer_format_version_is_refused(target: AureusPaths, tmp_path: Path) -> None:
    archive = tmp_path / "new.zip"
    craft(archive, {"config/identity.toml": b"x"}, version=EXPORT_FORMAT_VERSION + 1)
    with pytest.raises(PortabilityError, match="升级"):
        import_data(target, archive, CLOCK)


def test_not_a_zip_and_missing_manifest_are_refused(target: AureusPaths, tmp_path: Path) -> None:
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    with pytest.raises(PortabilityError, match="打不开"):
        import_data(target, broken, CLOCK)
    no_manifest = tmp_path / "plain.zip"
    with zipfile.ZipFile(no_manifest, "w") as bundle:
        bundle.writestr("config/identity.toml", b"x")
    with pytest.raises(PortabilityError, match="manifest"):
        import_data(target, no_manifest, CLOCK)
    assert not target.home.exists()


def test_force_without_backup_overwrites_and_keeps_no_backup(
    source: AureusPaths, target: AureusPaths, tmp_path: Path
) -> None:
    populate(target, "目标原有的记录")
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    result = import_data(target, archive, CLOCK, force=True, backup_first=False)
    assert records(target) == [RECORD]
    assert result.backup is None
    assert not list(target.snapshots_dir.glob("before-import-*.zip"))


def test_skipping_the_backup_does_not_bypass_the_overwrite_check(
    source: AureusPaths, target: AureusPaths, tmp_path: Path
) -> None:
    populate(target, "目标原有的记录")
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    with pytest.raises(PortabilityError, match="--force"):
        import_data(target, archive, CLOCK, backup_first=False)
    assert records(target) == ["目标原有的记录"]


# ---------- 导入加固（审查 H1、H2，F4） ----------


def craft_declaring(archive: Path, name: str, data: bytes, declared_size: int) -> None:
    """做一个 manifest 里声明的大小和真实内容不一样的包。"""
    manifest = {
        "export_format_version": EXPORT_FORMAT_VERSION,
        "files": [
            {"path": name, "size": declared_size, "sha256": hashlib.sha256(data).hexdigest()}
        ],
    }
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(MANIFEST_NAME, json.dumps(manifest))
        bundle.writestr(name, data)


def test_package_declaring_a_huge_file_is_refused_before_reading(
    target: AureusPaths, tmp_path: Path
) -> None:
    archive = tmp_path / "huge.zip"
    craft_declaring(archive, "data/aureus.db", b"x", declared_size=5 * 1024**3)
    with pytest.raises(PortabilityError, match="单个文件上限"):
        import_data(target, archive, CLOCK)
    assert not target.home.exists()


def test_package_over_the_total_size_limit_is_refused(
    target: AureusPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(portability, "MAX_TOTAL_BYTES", 5)
    archive = tmp_path / "big.zip"
    craft(archive, {"config/identity.toml": b"abc", "config/aureus.toml": b"def"})
    with pytest.raises(PortabilityError, match="总大小"):
        import_data(target, archive, CLOCK)
    assert not target.home.exists()


def test_package_with_too_many_entries_is_refused(
    target: AureusPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(portability, "MAX_ENTRY_COUNT", 2)
    archive = tmp_path / "many.zip"
    craft(archive, {f"data/diary/{index}.md": b"x" for index in range(3)})
    with pytest.raises(PortabilityError, match="超过上限"):
        import_data(target, archive, CLOCK)
    assert not target.home.exists()


def test_file_whose_real_size_differs_from_the_declared_size_is_refused(
    target: AureusPaths, tmp_path: Path
) -> None:
    archive = tmp_path / "lying.zip"
    craft_declaring(archive, "config/identity.toml", b"0123456789", declared_size=3)
    with pytest.raises(PortabilityError, match="大小"):
        import_data(target, archive, CLOCK)
    assert not target.home.exists()


def test_checksum_error_does_not_claim_the_package_proves_its_origin(
    target: AureusPaths, tmp_path: Path
) -> None:
    archive = tmp_path / "bad.zip"
    craft(archive, {"config/identity.toml": b"x"}, bad_digest=True)
    with pytest.raises(PortabilityError, match="只导入自己导出的包"):
        import_data(target, archive, CLOCK)


def fail_replacing_into_place(monkeypatch: pytest.MonkeyPatch, *, also_restoring: bool) -> None:
    """让「把暂存的 aureus.db 放到位」失败；also_restoring 为真时，放回旧文件也失败。"""
    real_replace = Path.replace

    def flaky_replace(self: Path, target: Path) -> Path:
        stage = self.parent.parent.name
        if self.name == "aureus.db" and (stage == "new" or (also_restoring and stage == "old")):
            raise PermissionError("模拟：文件被占用")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)


def test_failure_halfway_leaves_the_target_exactly_as_it_was(
    source: AureusPaths, target: AureusPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(target, "目标原有的记录")
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    target.identity_file.write_text("[facts]\nname = '目标自己的'\n", encoding="utf-8")
    original_identity = target.identity_file.read_text(encoding="utf-8")
    fail_replacing_into_place(monkeypatch, also_restoring=False)
    with pytest.raises(PortabilityError, match="放置文件时出错"):
        import_data(target, archive, CLOCK, force=True)
    monkeypatch.undo()
    assert records(target) == ["目标原有的记录"]
    assert target.identity_file.read_text(encoding="utf-8") == original_identity
    assert not list(target.data_dir.glob(".import-*"))


def test_failed_rollback_keeps_the_old_files_and_says_where(
    source: AureusPaths, target: AureusPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(target, "目标原有的记录")
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    fail_replacing_into_place(monkeypatch, also_restoring=True)
    with pytest.raises(PortabilityError, match="回滚也没做完"):
        import_data(target, archive, CLOCK, force=True)
    monkeypatch.undo()
    kept = list(target.data_dir.glob(".import-*/old/data/aureus.db"))
    assert len(kept) == 1  # 旧数据库没有丢，还在暂存目录里


def test_two_forced_imports_in_the_same_instant_keep_both_backups(
    source: AureusPaths, target: AureusPaths, tmp_path: Path
) -> None:
    populate(target, "目标原有的记录")
    archive = tmp_path / "out.zip"
    export_data(source, archive, CLOCK)
    first = import_data(target, archive, CLOCK, force=True)
    second = import_data(target, archive, CLOCK, force=True)
    assert first.backup is not None and second.backup is not None
    assert first.backup != second.backup
    assert first.backup.exists() and second.backup.exists()
