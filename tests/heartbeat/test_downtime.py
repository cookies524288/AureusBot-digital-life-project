from datetime import UTC, datetime, timedelta, timezone

import pytest

from aureus.heartbeat import EVENT_MESSAGE, EVENT_SHUT_DOWN, assess_downtime
from aureus.timeline import ActorType, TimelineRecord, VisibilityScope

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
CHINA = timezone(timedelta(hours=8))


def record_at(moment: datetime, event_type: str) -> TimelineRecord:
    return TimelineRecord(
        id=7,
        timestamp=moment,
        actor_type=ActorType.ME,
        event_type=event_type,
        visibility=VisibilityScope.PRIVATE,
    )


def test_empty_timeline_is_the_first_wake() -> None:
    downtime = assess_downtime(None, NOW)

    assert downtime.is_first_wake
    assert downtime.duration is None
    assert "第一次醒来" in downtime.describe(CHINA)


def test_after_an_orderly_shutdown_the_duration_is_exact() -> None:
    downtime = assess_downtime(
        record_at(NOW - timedelta(hours=10, minutes=5), EVENT_SHUT_DOWN), NOW
    )

    assert downtime.shutdown_recorded
    assert downtime.duration == timedelta(hours=10, minutes=5)
    assert (
        downtime.describe(CHINA)
        == "上次在 2026-11-17 07:55（UTC+08:00）关闭，停了 10 小时 5 分钟。"
    )


def test_without_a_shutdown_record_the_duration_is_an_upper_bound() -> None:
    downtime = assess_downtime(record_at(NOW - timedelta(days=2), EVENT_MESSAGE), NOW)

    assert not downtime.shutdown_recorded
    assert downtime.duration == timedelta(days=2)
    description = downtime.describe(CHINA)
    assert "没有留下关闭记录" in description
    assert "最多停了 2 天" in description


def test_last_record_in_the_future_means_unknown_not_negative() -> None:
    downtime = assess_downtime(record_at(NOW + timedelta(hours=1), EVENT_SHUT_DOWN), NOW)

    assert downtime.duration is None
    assert not downtime.is_first_wake
    assert "不知道停了多久" in downtime.describe(CHINA)


def test_restart_within_the_same_instant_is_zero_not_unknown() -> None:
    downtime = assess_downtime(record_at(NOW, EVENT_SHUT_DOWN), NOW)

    assert downtime.duration == timedelta(0)
    assert "不到 1 分钟" in downtime.describe(CHINA)


@pytest.mark.parametrize(
    "last",
    [None, record_at(NOW - timedelta(hours=1), EVENT_SHUT_DOWN), record_at(NOW, EVENT_MESSAGE)],
)
def test_metadata_only_uses_json_values(last: TimelineRecord | None) -> None:
    import json

    metadata = assess_downtime(last, NOW).to_metadata()

    assert json.loads(json.dumps(metadata)) == metadata
    assert metadata["woke_at"] == NOW.isoformat()


def test_short_downtime_reads_naturally() -> None:
    downtime = assess_downtime(record_at(NOW - timedelta(seconds=20), EVENT_SHUT_DOWN), NOW)

    assert downtime.describe(CHINA).endswith("停了不到 1 分钟。")
