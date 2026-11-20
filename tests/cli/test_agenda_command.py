"""agenda 命令的测试（路线图 2.7，D74 第 7 条）：查看和取消，取消不算成败。"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.agenda import AgendaStore, ItemStatus
from aureus.cli.agenda_command import run_agenda_command
from aureus.cli.inspect_commands import EXIT_FAILURE, EXIT_OK, NO_DATABASE_MESSAGE
from aureus.common import AgendaConfig, AureusPaths, ManualClock
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    return paths


def _seed(paths: AureusPaths) -> list[int]:
    store = AgendaStore(paths.database_file, ManualClock(NOW), AgendaConfig())
    store.open()
    try:
        first = store.add(1, "提醒他开会", due_at=NOW, source_record_id=None, feedback_id=None)
        second = store.add(1, "推荐一本书", due_at=None, source_record_id=None, feedback_id=None)
        assert first is not None and second is not None
        store.finish(second.id, ItemStatus.DONE)
        return [first.id, second.id]
    finally:
        store.close()


def _run(paths: AureusPaths, *, all_items: bool = False, cancel: int | None = None):
    lines: list[str] = []
    args = argparse.Namespace(all=all_items, limit=30, cancel=cancel)
    return run_agenda_command(args, paths, lines.append), "\n".join(lines)


def test_without_database_it_says_so(paths: AureusPaths) -> None:
    assert _run(paths) == (EXIT_OK, NO_DATABASE_MESSAGE)
    assert not paths.database_file.exists()


def test_lists_open_items_by_default_and_all_with_flag(paths: AureusPaths) -> None:
    open_id, done_id = _seed(paths)
    code, text = _run(paths)
    assert code == EXIT_OK
    assert f"#{open_id} [未完成] 提醒他开会" in text and f"#{done_id}" not in text
    _, everything = _run(paths, all_items=True)
    assert f"#{done_id} [做完了] 推荐一本书" in everything


def test_cancel_marks_cancelled_and_records_the_user(paths: AureusPaths) -> None:
    open_id, done_id = _seed(paths)
    code, text = _run(paths, cancel=open_id)
    assert code == EXIT_OK and "已取消" in text

    store = AgendaStore(paths.database_file, ManualClock(NOW), AgendaConfig())
    store.open()
    try:
        item = store.get(open_id)
        assert item is not None and item.status is ItemStatus.CANCELLED
    finally:
        store.close()
    timeline = TimelineDatabase(paths.database_file, ManualClock(NOW))
    timeline.open()
    try:
        [record] = timeline.query(TimelineQuery(event_type="agenda_closed", limit=10))
        assert record.actor_type is ActorType.USER
    finally:
        timeline.close()

    # 已经结束的、不存在的都取消不了
    assert _run(paths, cancel=done_id)[0] == EXIT_FAILURE
    assert _run(paths, cancel=999)[0] == EXIT_FAILURE
