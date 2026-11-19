"""persona 命令的测试（路线图 1.9）：临时数据根目录、编造的人设，不调用模型。"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.cli.inspect_commands import EXIT_OK, NO_DATABASE_MESSAGE
from aureus.cli.main import main
from aureus.cli.persona_command import run_persona_command
from aureus.common import AureusPaths, ManualClock
from aureus.identity import Identity, IdentityHistory

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path: Path) -> AureusPaths:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    return paths


def make_identity(expression_style: str) -> Identity:
    return Identity.model_validate(
        {
            "facts": {"name": "小绿", "birthday": "2008-01-01"},
            "user": {"name": "阿明"},
            "persona": {"immutable": "温和。", "expression_style": expression_style},
        }
    )


def record_versions(paths: AureusPaths, *styles: str) -> None:
    history = IdentityHistory(paths.database_file, ManualClock(NOW))
    history.open()
    try:
        for style in styles:
            history.sync_from_file(make_identity(style))
    finally:
        history.close()


def collect(
    paths: AureusPaths, *, layer: str | None = None, limit: int = 20, full: bool = False
) -> tuple[int, str]:
    lines: list[str] = []
    args = argparse.Namespace(layer=layer, limit=limit, full=full)
    code = run_persona_command(args, paths, lines.append)
    return code, "\n".join(lines)


def test_without_a_database_it_says_so_and_creates_nothing(paths: AureusPaths) -> None:
    code, output = collect(paths)

    assert code == EXIT_OK
    assert output == NO_DATABASE_MESSAGE
    assert not paths.database_file.exists()


def test_one_layer_shows_old_and_new_versions_in_order(paths: AureusPaths) -> None:
    record_versions(paths, "句子短。", "多用语气词。")

    code, output = collect(paths, layer="expression_style")

    assert code == EXIT_OK
    assert "表达风格最新的 2 个版本" in output
    assert "最后一个是现在生效的" in output
    first, second = output.splitlines()[1:]
    assert "表达风格（初始版本）：句子短。" in first
    assert "表达风格（用户修改）：多用语气词。" in second


def test_all_layers_are_listed_without_claiming_the_last_is_current(paths: AureusPaths) -> None:
    record_versions(paths, "句子短。")

    code, output = collect(paths)

    assert code == EXIT_OK
    assert "各层混在一起" in output
    assert "现在生效的" not in output
    assert "身份事实（初始版本）" in output
    assert "可变人格（初始版本）：（空）" in output


def test_the_limit_keeps_the_latest(paths: AureusPaths) -> None:
    record_versions(paths, "一。", "二。", "三。")

    _, output = collect(paths, layer="expression_style", limit=1)

    assert "三。" in output
    assert "二。" not in output


def test_full_shows_every_line(paths: AureusPaths) -> None:
    record_versions(paths, "第一行\n第二行")

    _, output = collect(paths, layer="expression_style", full=True)

    assert "    第一行\n    第二行" in output


def test_the_command_line_accepts_the_options(
    paths: AureusPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    record_versions(paths, "句子短。")

    code = main(
        ["--home", str(paths.home), "persona", "--layer", "expression_style", "--limit", "5"]
    )

    assert code == EXIT_OK
    assert "表达风格（初始版本）：句子短。" in capsys.readouterr().out
