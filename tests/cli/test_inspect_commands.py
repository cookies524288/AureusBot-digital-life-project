"""timeline 和 usage 命令的测试：临时数据根目录、编造的假记录，不调用模型。"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.cli.inspect_commands import (
    CONTENT_PREVIEW_CHARS,
    EXIT_FAILURE,
    EXIT_OK,
    NO_DATABASE_MESSAGE,
    positive_int,
    run_timeline_command,
    run_usage_command,
)
from aureus.cli.main import main
from aureus.common import AureusPaths, ManualClock
from aureus.organs import CallAttempt
from aureus.safety import Ledger
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope

LAST_YEAR = datetime(2020, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    return paths


def timeline_args(limit: int = 20, event_type: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(limit=limit, event_type=event_type)


def add_records(paths: AureusPaths, *records: tuple[str, str]) -> None:
    """往时间线追加 (事件类型, 内容) 记录。"""
    timeline = TimelineDatabase(paths.database_file)
    timeline.open()
    try:
        for event_type, content in records:
            timeline.append(
                event_type=event_type,
                content=content,
                actor_type=ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
            )
    finally:
        timeline.close()


def add_attempt(
    paths: AureusPaths,
    *,
    organ_id: str = "replyer",
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    cost: float | None = None,
    succeeded: bool = True,
    clock: ManualClock | None = None,
) -> None:
    ledger = Ledger(paths.database_file, clock)
    ledger.open()
    try:
        attempt = CallAttempt(
            model="fake-model",
            provider="fake",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            succeeded=succeeded,
            failure_reason=None if succeeded else "测试失败",
            cost=cost,
        )
        ledger.record(organ_id, [attempt])
    finally:
        ledger.close()


def collect(run, *arguments) -> tuple[int, str]:
    lines: list[str] = []
    code = run(*arguments, lines.append)
    return code, "\n".join(lines)


# ---------- timeline ----------


def test_timeline_without_database_says_so_and_creates_nothing(paths: AureusPaths) -> None:
    code, output = collect(run_timeline_command, timeline_args(), paths)
    assert code == EXIT_OK
    assert output == NO_DATABASE_MESSAGE
    # 只读命令不能为了查看而新建一个空数据库。
    assert not paths.database_file.exists()


def test_timeline_shows_only_the_latest_records_in_order(paths: AureusPaths) -> None:
    add_records(paths, *[("message", f"第 {number} 条") for number in range(1, 6)])
    code, output = collect(run_timeline_command, timeline_args(limit=3), paths)
    assert code == EXIT_OK
    lines = output.splitlines()
    assert "共 5 条记录" in lines[0]
    assert "最新 3 条" in lines[0]
    assert [line.split("]")[0] for line in lines[1:]] == ["[3", "[4", "[5"]
    assert "第 1 条" not in output


def test_timeline_filters_by_event_type(paths: AureusPaths) -> None:
    add_records(paths, ("message", "聊天内容"), ("woke_up", "醒来了"), ("message", "又一句"))
    code, output = collect(run_timeline_command, timeline_args(event_type="message"), paths)
    assert code == EXIT_OK
    assert "聊天内容" in output
    assert "又一句" in output
    assert "醒来了" not in output


def test_timeline_shows_source_and_visibility(paths: AureusPaths) -> None:
    add_records(paths, ("message", "内容"))
    _, output = collect(run_timeline_command, timeline_args(), paths)
    assert "me message（private）" in output


def test_timeline_truncates_long_content_to_one_line(paths: AureusPaths) -> None:
    add_records(paths, ("message", "很长\n" * 100))
    _, output = collect(run_timeline_command, timeline_args(), paths)
    record_lines = output.splitlines()[1:]
    assert len(record_lines) == 1
    assert record_lines[0].endswith("……")
    assert "很长" * CONTENT_PREVIEW_CHARS not in record_lines[0]


def test_timeline_with_empty_database(paths: AureusPaths) -> None:
    add_records(paths)
    code, output = collect(run_timeline_command, timeline_args(), paths)
    assert code == EXIT_OK
    assert "共 0 条记录" in output


def test_timeline_unreadable_database_fails_with_one_line(
    paths: AureusPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.database_file.parent.mkdir(parents=True, exist_ok=True)
    paths.database_file.write_bytes(b"this is not a sqlite database" * 50)
    code, _ = collect(run_timeline_command, timeline_args(), paths)
    assert code == EXIT_FAILURE
    assert "读不了时间线" in capsys.readouterr().err


# ---------- usage ----------


def test_usage_without_database_says_so_and_creates_nothing(paths: AureusPaths) -> None:
    code, output = collect(run_usage_command, paths)
    assert code == EXIT_OK
    assert output == NO_DATABASE_MESSAGE
    assert not paths.database_file.exists()


def test_usage_sums_tokens_and_splits_by_organ(paths: AureusPaths) -> None:
    add_attempt(paths, organ_id="replyer", prompt_tokens=100, completion_tokens=20, cost=0.5)
    add_attempt(paths, organ_id="replyer", prompt_tokens=200, completion_tokens=30, cost=0.25)
    add_attempt(paths, organ_id="planner", prompt_tokens=10, completion_tokens=5, cost=0.0)
    code, output = collect(run_usage_command, paths)
    assert code == EXIT_OK
    assert "3 次请求" in output
    assert "输入 310 + 输出 55 = 365" in output
    assert "replyer：2 次（失败 0），输入 300，输出 50，费用 0.7500 元" in output
    assert "planner：1 次（失败 0），输入 10，输出 5，费用 0.0000 元" in output
    assert "费用：0.7500 元" in output


def test_usage_unknown_cost_is_not_shown_as_free(paths: AureusPaths) -> None:
    add_attempt(paths, cost=0.5)
    add_attempt(paths, cost=None)
    _, output = collect(run_usage_command, paths)
    assert "费用：未知" in output
    assert "0.5000" not in output


def test_usage_counts_failures(paths: AureusPaths) -> None:
    add_attempt(paths)
    add_attempt(paths, succeeded=False)
    _, output = collect(run_usage_command, paths)
    assert "2 次请求，失败 1 次" in output
    assert "replyer：2 次（失败 1）" in output


def test_usage_ignores_other_months(paths: AureusPaths) -> None:
    add_attempt(paths, prompt_tokens=999, completion_tokens=1, clock=ManualClock(LAST_YEAR))
    add_attempt(paths, prompt_tokens=100, completion_tokens=20)
    _, output = collect(run_usage_command, paths)
    assert "1 次请求" in output
    assert "输入 100 + 输出 20 = 120" in output


def test_usage_with_only_old_records_shows_zero(paths: AureusPaths) -> None:
    add_attempt(paths, clock=ManualClock(LAST_YEAR))
    code, output = collect(run_usage_command, paths)
    assert code == EXIT_OK
    assert "0 次请求" in output
    assert "= 0" in output


def test_usage_reports_limit_and_percentage(paths: AureusPaths) -> None:
    paths.runtime_config_file.write_text("[safety]\nmonthly_token_limit = 2000\n", encoding="utf-8")
    add_attempt(paths, prompt_tokens=900, completion_tokens=100)
    _, output = collect(run_usage_command, paths)
    assert "月度上限：2000，已用 50.0%" in output


def test_usage_without_limit_tells_how_to_set_one(paths: AureusPaths) -> None:
    add_attempt(paths)
    _, output = collect(run_usage_command, paths)
    assert "没有设置" in output
    assert "monthly_token_limit" in output


def test_usage_over_the_limit_shows_more_than_100_percent(paths: AureusPaths) -> None:
    paths.runtime_config_file.write_text("[safety]\nmonthly_token_limit = 100\n", encoding="utf-8")
    add_attempt(paths, prompt_tokens=150, completion_tokens=0)
    _, output = collect(run_usage_command, paths)
    assert "已用 150.0%" in output


def test_usage_bad_config_fails_without_leaking_values(
    paths: AureusPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.runtime_config_file.write_text("[safety]\nmonthly_token_limit = 0\n", encoding="utf-8")
    add_attempt(paths)
    code, _ = collect(run_usage_command, paths)
    assert code == EXIT_FAILURE
    assert "monthly_token_limit" in capsys.readouterr().err


def test_usage_unreadable_database_fails_with_one_line(
    paths: AureusPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.database_file.parent.mkdir(parents=True, exist_ok=True)
    paths.database_file.write_bytes(b"this is not a sqlite database" * 50)
    code, _ = collect(run_usage_command, paths)
    assert code == EXIT_FAILURE
    assert "读不了账本" in capsys.readouterr().err


# ---------- 命令行解析 ----------


@pytest.mark.parametrize("text", ["0", "-3", "abc", "1.5", ""])
def test_positive_int_rejects_bad_values(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int(text)


def test_positive_int_accepts_positive_values() -> None:
    assert positive_int("7") == 7


def test_main_dispatches_timeline_and_usage(
    paths: AureusPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    add_records(paths, ("message", "主入口测试"))
    add_attempt(paths, prompt_tokens=1, completion_tokens=2)
    assert main(["--home", str(paths.home), "timeline", "--limit", "5"]) == EXIT_OK
    assert "主入口测试" in capsys.readouterr().out
    assert main(["--home", str(paths.home), "usage"]) == EXIT_OK
    assert "输入 1 + 输出 2 = 3" in capsys.readouterr().out


def test_main_rejects_non_positive_limit(paths: AureusPaths) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--home", str(paths.home), "timeline", "--limit", "0"])
    assert exit_info.value.code == 2
