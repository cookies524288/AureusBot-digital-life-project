# export 和 import 命令的测试：走命令行入口 main，临时数据根目录，不含任何真实数据。
from pathlib import Path

import pytest

from aureus.cli.main import main
from aureus.common import AureusPaths
from aureus.identity import IdentityService
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery

Capture = pytest.CaptureFixture[str]
RECORD = "命令行导出前的记录"


def make_home(home: Path, record: str = RECORD) -> AureusPaths:
    paths = AureusPaths(home)
    paths.ensure_directories()
    paths.identity_file.write_text("# fake identity", encoding="utf-8")
    database = TimelineDatabase(paths.database_file)
    database.open()
    try:
        database.append("test", record, ActorType.ME)
    finally:
        database.close()
    return paths


def records(paths: AureusPaths) -> list[str]:
    database = TimelineDatabase(paths.database_file)
    database.open()
    try:
        return [item.content for item in database.query(TimelineQuery())]
    finally:
        database.close()


def test_export_then_import_round_trip(tmp_path: Path, capsys: Capture) -> None:
    source = make_home(tmp_path / "source")
    archive = tmp_path / "out.zip"
    assert main(["--home", str(source.home), "export", str(archive)]) == 0
    output = capsys.readouterr().out
    assert archive.is_file()
    assert "没有密钥" in output

    target = AureusPaths(tmp_path / "target")
    assert main(["--home", str(target.home), "import", str(archive)]) == 0
    assert "已导入" in capsys.readouterr().out
    assert records(target) == [RECORD]


def test_export_refuses_existing_file_with_exit_code_1(tmp_path: Path, capsys: Capture) -> None:
    source = make_home(tmp_path / "source")
    archive = tmp_path / "out.zip"
    archive.write_bytes(b"keep me")
    assert main(["--home", str(source.home), "export", str(archive)]) == 1
    assert "已经存在" in capsys.readouterr().err
    assert archive.read_bytes() == b"keep me"


def test_import_without_force_refuses_and_keeps_data(tmp_path: Path, capsys: Capture) -> None:
    source = make_home(tmp_path / "source")
    archive = tmp_path / "out.zip"
    assert main(["--home", str(source.home), "export", str(archive)]) == 0
    target = make_home(tmp_path / "target", "目标原有的记录")
    capsys.readouterr()
    assert main(["--home", str(target.home), "import", str(archive)]) == 1
    assert "--force" in capsys.readouterr().err
    assert records(target) == ["目标原有的记录"]


def test_import_with_force_reports_the_backup(tmp_path: Path, capsys: Capture) -> None:
    source = make_home(tmp_path / "source")
    archive = tmp_path / "out.zip"
    assert main(["--home", str(source.home), "export", str(archive)]) == 0
    target = make_home(tmp_path / "target", "目标原有的记录")
    capsys.readouterr()
    assert main(["--home", str(target.home), "import", str(archive), "--force"]) == 0
    assert "已备份到" in capsys.readouterr().out
    assert records(target) == [RECORD]
    assert list(target.snapshots_dir.glob("before-import-*.zip"))


def test_import_is_refused_while_an_instance_runs(tmp_path: Path, capsys: Capture) -> None:
    source = make_home(tmp_path / "source")
    archive = tmp_path / "out.zip"
    assert main(["--home", str(source.home), "export", str(archive)]) == 0
    target = AureusPaths(tmp_path / "target")
    capsys.readouterr()
    with IdentityService(target).acquire_lock():
        assert main(["--home", str(target.home), "import", str(archive)]) == 1
    assert "另一个" in capsys.readouterr().err
    assert not target.database_file.exists()


def test_import_missing_file_fails_with_one_line(tmp_path: Path, capsys: Capture) -> None:
    target = AureusPaths(tmp_path / "target")
    assert main(["--home", str(target.home), "import", str(tmp_path / "nope.zip")]) == 1
    assert "打不开" in capsys.readouterr().err
    assert not target.home.exists()


def test_empty_archive_name_is_rejected_by_the_parser(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--home", str(tmp_path / "home"), "export", "  "])
    assert exit_info.value.code == 2


def test_no_backup_with_force_overwrites_without_a_backup(tmp_path: Path, capsys: Capture) -> None:
    source = make_home(tmp_path / "source")
    archive = tmp_path / "out.zip"
    assert main(["--home", str(source.home), "export", str(archive)]) == 0
    target = make_home(tmp_path / "target", "目标原有的记录")
    capsys.readouterr()
    command = ["--home", str(target.home), "import", str(archive), "--force", "--no-backup"]
    assert main(command) == 0
    assert "没有备份" in capsys.readouterr().out
    assert records(target) == [RECORD]
    assert not list(target.snapshots_dir.glob("before-import-*.zip"))


def test_no_backup_without_force_is_refused(tmp_path: Path, capsys: Capture) -> None:
    source = make_home(tmp_path / "source")
    archive = tmp_path / "out.zip"
    assert main(["--home", str(source.home), "export", str(archive)]) == 0
    target = make_home(tmp_path / "target", "目标原有的记录")
    capsys.readouterr()
    assert main(["--home", str(target.home), "import", str(archive), "--no-backup"]) == 1
    assert "--force" in capsys.readouterr().err
    assert records(target) == ["目标原有的记录"]
