"""观察台页面的冒烟测试：用 Streamlit 自带的 AppTest 跑一遍，不开浏览器、不占端口（D64）。"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.body import BodySample, BodySampleStore
from aureus.cli.monitor_command import PAGE_FILE
from aureus.common import AureusPaths, ManualClock

streamlit_testing = pytest.importorskip("streamlit.testing.v1")

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def run_page(paths: AureusPaths, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.argv", ["page.py", "--home", str(paths.home)])
    app = streamlit_testing.AppTest.from_file(str(PAGE_FILE), default_timeout=30)
    return app.run()


def test_page_shows_a_message_when_there_is_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    app = run_page(paths, monkeypatch)
    assert not app.exception
    assert any("找不到数据库" in warning.value for warning in app.warning)


def test_page_renders_an_empty_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(paths.database_file).close()
    app = run_page(paths, monkeypatch)
    assert not app.exception
    assert any("还没有身体信号样本" in info.value for info in app.info)


def test_page_renders_a_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    store = BodySampleStore(paths.database_file, ManualClock(NOW))
    store.open()
    store.append(
        BodySample(
            timestamp=NOW,
            energy_tier="节省",
            energy_fraction=0.35,
            used_today=100,
            daily_quota=500,
            fatigue_level=None,
            stress_level=0.2,
            backlog=0,
            night=False,
            focus="用户",
            inbox_count=2,
        )
    )
    store.close()
    app = run_page(paths, monkeypatch)
    assert not app.exception
    labels = {metric.label: metric.value for metric in app.metric}
    assert labels["精力"] == "节省"
    assert labels["疲劳"] == "已关闭"  # 关掉的信号显示「已关闭」，不装成 0
    assert labels["收件箱"] == "2 条"
