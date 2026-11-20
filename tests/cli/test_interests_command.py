"""兴趣命令的列表、结束和并行运行；全部使用临时数据（D79）。"""

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest

from aureus.agenda import FindingStore, InterestStore
from aureus.cli.interests_command import run_interests_command
from aureus.cli.main import main
from aureus.common import AureusPaths, InterestsConfig, ManualClock
from aureus.identity.service import IdentityService
from tests.heartbeat.test_judging_flow import NOW


@pytest.fixture
def paths(tmp_path: Path) -> Iterator[AureusPaths]:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    yield paths


def _run(paths: AureusPaths, remove: int | None = None) -> tuple[int, str]:
    lines: list[str] = []
    code = run_interests_command(argparse.Namespace(remove=remove), paths, lines.append)
    return code, "\n".join(lines)


def _seed(paths: AureusPaths) -> int:
    clock = ManualClock(NOW)
    interests = InterestStore(paths.database_file, clock, InterestsConfig())
    findings = FindingStore(paths.database_file, clock)
    interests.open()
    findings.open()
    try:
        item = interests.add("天文", "seed")
        assert item is not None
        interests.add("音乐", "self", 1)
        for i in range(7):
            finding = findings.add(item.id, f"收获{i}", f"标题{i}", "https://example.com")
            if i == 6:
                findings.mark_shared(finding.id, 100, NOW)
        return item.id
    finally:
        findings.close()
        interests.close()


def test_lists_active_interests_and_only_five_recent_findings(paths: AureusPaths) -> None:
    _seed(paths)
    code, text = _run(paths)
    assert code == 0 and "[起始] 天文" in text and "[自己加的] 音乐" in text
    assert "2026-11-17" in text and "[已分享] 收获6" in text and "[未分享] 收获5" in text
    assert "标题6" in text and "收获0" not in text and "收获1" not in text


def test_remove_and_unknown_or_ended_id(paths: AureusPaths) -> None:
    item_id = _seed(paths)
    assert _run(paths, item_id)[0] == 0
    assert _run(paths, item_id)[0] != 0 and _run(paths, 999)[0] != 0
    store = InterestStore(paths.database_file, ManualClock(NOW), InterestsConfig())
    store.open()
    try:
        item = store.get(item_id)
        assert item is not None and item.end_reason == "user_removed"
        assert store.sync_seeds(["天文"]) == (0, 0)
        assert all(item.text != "天文" for item in store.active())
    finally:
        store.close()


def test_missing_database_is_not_created(paths: AureusPaths) -> None:
    assert _run(paths)[0] == 0
    assert _run(paths, 1)[0] != 0
    assert not paths.database_file.exists()


def test_cli_works_while_instance_lock_is_held(paths: AureusPaths) -> None:
    item_id = _seed(paths)
    with IdentityService(paths).acquire_lock():
        assert main(["--home", str(paths.home), "interests"]) == 0
        assert main(["--home", str(paths.home), "interests", "--remove", str(item_id)]) == 0
