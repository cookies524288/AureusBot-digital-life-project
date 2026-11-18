"""急停标记文件和权限检查的测试：只用临时目录，不碰真实数据。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.safety import (
    ACTION_CALL_ORGAN,
    ACTION_REPLY_TO_USER,
    DEFAULT_HALT_REASON,
    EmergencyStop,
    PermissionChecker,
    PermissionLevel,
)

NOW = datetime(2026, 11, 18, 3, 0, tzinfo=UTC)


@pytest.fixture
def stop(tmp_path: Path) -> EmergencyStop:
    return EmergencyStop(tmp_path / "data" / "halt.json", ManualClock(NOW))


# ---------- 标记文件 ----------


def test_no_marker_means_not_halted(stop: EmergencyStop) -> None:
    assert stop.status().halted is False


def test_engage_records_time_and_reason(stop: EmergencyStop) -> None:
    assert stop.engage("我要睡了") is True
    status = stop.status()
    assert status.halted is True
    assert status.reason == "我要睡了"
    assert status.since == NOW


def test_blank_reason_gets_the_default(stop: EmergencyStop) -> None:
    stop.engage("   ")
    assert stop.status().reason == DEFAULT_HALT_REASON


def test_engaging_twice_keeps_the_first_record(stop: EmergencyStop) -> None:
    stop.engage("第一次")
    assert stop.engage("第二次") is False
    assert stop.status().reason == "第一次"


def test_release_clears_the_halt(stop: EmergencyStop) -> None:
    stop.engage()
    assert stop.release() is True
    assert stop.status().halted is False
    assert stop.release() is False  # 本来就没有急停


def test_no_temporary_file_is_left_behind(stop: EmergencyStop, tmp_path: Path) -> None:
    stop.engage()
    assert [path.name for path in (tmp_path / "data").iterdir()] == ["halt.json"]


@pytest.mark.parametrize("content", ["", "不是 JSON", "[1, 2]", '{"at": "坏时间", "reason": "x"}'])
def test_unreadable_marker_still_counts_as_halted(
    stop: EmergencyStop, tmp_path: Path, content: str
) -> None:
    # 朝安全方向倒：标记在，但内容读不懂，也算急停中。
    marker = tmp_path / "data" / "halt.json"
    marker.parent.mkdir()
    marker.write_text(content, encoding="utf-8")
    status = stop.status()
    assert status.halted is True
    assert status.since is None


def test_marker_that_cannot_be_read_counts_as_halted(tmp_path: Path) -> None:
    marker = tmp_path / "halt.json"
    marker.mkdir()  # 同名的是文件夹，读不了
    status = EmergencyStop(marker, ManualClock(NOW)).status()
    assert status.halted is True
    assert "读不了" in status.reason


# ---------- 权限检查 ----------


def make_checker(stop: EmergencyStop, **levels: PermissionLevel) -> PermissionChecker:
    return PermissionChecker(stop, levels)


def test_registered_actions_are_allowed_when_not_halted(stop: EmergencyStop) -> None:
    checker = PermissionChecker(stop)
    assert checker.check(ACTION_CALL_ORGAN).allowed
    assert checker.check(ACTION_REPLY_TO_USER).allowed


def test_unregistered_action_is_rejected(stop: EmergencyStop) -> None:
    decision = PermissionChecker(stop).check("format_the_disk")
    assert decision.allowed is False
    assert "没有登记" in decision.reason


def test_l3_is_rejected_until_approval_exists(stop: EmergencyStop) -> None:
    decision = make_checker(stop, spend=PermissionLevel.NEEDS_APPROVAL).check("spend")
    assert decision.allowed is False
    assert "L3" in decision.reason


def test_l4_is_always_rejected(stop: EmergencyStop) -> None:
    decision = make_checker(stop, forbidden=PermissionLevel.FORBIDDEN).check("forbidden")
    assert decision.allowed is False
    assert "L4" in decision.reason


def test_halt_blocks_everything_above_l0(stop: EmergencyStop) -> None:
    checker = make_checker(
        stop,
        inner=PermissionLevel.FREE,
        daily=PermissionLevel.ROUTINE,
        undoable=PermissionLevel.TRACEABLE,
    )
    stop.engage("测试")
    assert checker.check("inner").allowed is True
    assert checker.check("daily").allowed is False
    assert checker.check("undoable").allowed is False
    assert "急停中" in checker.check("daily").reason


def test_halt_is_seen_immediately_and_resume_restores(stop: EmergencyStop) -> None:
    checker = PermissionChecker(stop)
    stop.engage()
    assert checker.check(ACTION_CALL_ORGAN).allowed is False
    stop.release()
    assert checker.check(ACTION_CALL_ORGAN).allowed is True


def test_a_failing_check_rejects(stop: EmergencyStop, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError("没想到的错")

    monkeypatch.setattr(stop, "status", broken)
    decision = PermissionChecker(stop).check(ACTION_CALL_ORGAN)
    assert decision.allowed is False
    assert "出错" in decision.reason
